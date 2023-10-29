import torch
import torch.nn as nn
import torch.nn.functional as F

from utils.box_ops import bbox2dist, bbox_iou
from utils.distributed_utils import get_world_size, is_dist_avail_and_initialized

from .matcher import TaskAlignedAssigner


class Criterion(object):
    def __init__(self, cfg, device, num_classes=80):
        # --------------- Basic parameters ---------------
        self.cfg = cfg
        self.device = device
        self.num_classes = num_classes
        self.reg_max = cfg['reg_max']
        self.use_dfl = cfg['reg_max'] > 1
        # --------------- Loss config ---------------
        self.loss_cls_weight = cfg['loss_cls_weight']
        self.loss_box_weight = cfg['loss_box_weight']
        self.loss_dfl_weight = cfg['loss_dfl_weight']
        # --------------- Matcher config ---------------
        self.matcher_hpy = cfg['matcher_hpy']
        self.matcher = TaskAlignedAssigner(num_classes     = num_classes,
                                           topk_candidates = self.matcher_hpy['topk_candidates'],
                                           alpha           = self.matcher_hpy['alpha'],
                                           beta            = self.matcher_hpy['beta']
                                           )

    def loss_classes(self, pred_cls, gt_score):
        # compute bce loss
        loss_cls = F.binary_cross_entropy_with_logits(pred_cls, gt_score, reduction='none')

        return loss_cls
    
    def loss_bboxes(self, pred_box, gt_box, bbox_weight):
        # regression loss
        ious = bbox_iou(pred_box, gt_box, xywh=False, CIoU=True)
        loss_box = (1.0 - ious.squeeze(-1)) * bbox_weight

        return loss_box
    
    def loss_dfl(self, pred_reg, gt_box, anchor, stride, bbox_weight=None):
        # rescale coords by stride
        gt_box_s = gt_box / stride
        anchor_s = anchor / stride

        # compute deltas
        gt_ltrb_s = bbox2dist(anchor_s, gt_box_s, self.cfg['reg_max'] - 1)

        gt_left = gt_ltrb_s.to(torch.long)
        gt_right = gt_left + 1

        weight_left = gt_right.to(torch.float) - gt_ltrb_s
        weight_right = 1 - weight_left

        # loss left
        loss_left = F.cross_entropy(
            pred_reg.view(-1, self.cfg['reg_max']),
            gt_left.view(-1),
            reduction='none').view(gt_left.shape) * weight_left
        # loss right
        loss_right = F.cross_entropy(
            pred_reg.view(-1, self.cfg['reg_max']),
            gt_right.view(-1),
            reduction='none').view(gt_left.shape) * weight_right

        loss_dfl = (loss_left + loss_right).mean(-1)
        
        if bbox_weight is not None:
            loss_dfl *= bbox_weight

        return loss_dfl

    def __call__(self, outputs, targets, epoch=0):        
        """
            outputs['pred_cls']: List(Tensor) [B, M, C]
            outputs['pred_regs']: List(Tensor) [B, M, 4*(reg_max+1)]
            outputs['pred_boxs']: List(Tensor) [B, M, 4]
            outputs['anchors']: List(Tensor) [M, 2]
            outputs['strides']: List(Int) [8, 16, 32] output stride
            outputs['stride_tensor']: List(Tensor) [M, 1]
            targets: (List) [dict{'boxes': [...], 
                                 'labels': [...], 
                                 'orig_size': ...}, ...]
        """
        bs = outputs['pred_cls'][0].shape[0]
        device = outputs['pred_cls'][0].device
        strides = outputs['stride_tensor']
        anchors = outputs['anchors']
        anchors = torch.cat(anchors, dim=0)
        num_anchors = anchors.shape[0]

        # preds: [B, M, C]
        cls_preds = torch.cat(outputs['pred_cls'], dim=1)
        reg_preds = torch.cat(outputs['pred_reg'], dim=1)
        box_preds = torch.cat(outputs['pred_box'], dim=1)
        
        # --------------- label assignment ---------------
        gt_score_targets = []
        gt_bbox_targets = []
        fg_masks = []
        for batch_idx in range(bs):
            tgt_labels = targets[batch_idx]["labels"].to(device)     # [Mp,]
            tgt_boxs = targets[batch_idx]["boxes"].to(device)        # [Mp, 4]

            # check target
            if len(tgt_labels) == 0 or tgt_boxs.max().item() == 0.:
                # There is no valid gt
                fg_mask = cls_preds.new_zeros(1, num_anchors).bool()               #[1, M,]
                gt_score = cls_preds.new_zeros((1, num_anchors, self.num_classes)) #[1, M, C]
                gt_box = cls_preds.new_zeros((1, num_anchors, 4))                  #[1, M, 4]
            else:
                tgt_labels = tgt_labels[None, :, None]      # [1, Mp, 1]
                tgt_boxs = tgt_boxs[None]                   # [1, Mp, 4]
                (
                    _,
                    gt_box,     # [1, M, 4]
                    gt_score,   # [1, M, C]
                    fg_mask,    # [1, M,]
                    _
                ) = self.matcher(
                    pd_scores = cls_preds[batch_idx:batch_idx+1].detach().sigmoid(), 
                    pd_bboxes = box_preds[batch_idx:batch_idx+1].detach(),
                    anc_points = anchors,
                    gt_labels = tgt_labels,
                    gt_bboxes = tgt_boxs
                    )
            gt_score_targets.append(gt_score)
            gt_bbox_targets.append(gt_box)
            fg_masks.append(fg_mask)

        # List[B, 1, M, C] -> Tensor[B, M, C] -> Tensor[BM, C]
        fg_masks = torch.cat(fg_masks, 0).view(-1)                                    # [BM,]
        gt_score_targets = torch.cat(gt_score_targets, 0).view(-1, self.num_classes)  # [BM, C]
        gt_bbox_targets = torch.cat(gt_bbox_targets, 0).view(-1, 4)                   # [BM, 4]
        num_fgs = gt_score_targets.sum()
        
        # Average loss normalizer across all the GPUs
        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_fgs)
        num_fgs = (num_fgs / get_world_size()).clamp(1.0)

        # ------------------ Classification loss ------------------
        cls_preds = cls_preds.view(-1, self.num_classes)
        loss_cls = self.loss_classes(cls_preds, gt_score_targets)
        loss_cls = loss_cls.sum() / num_fgs

        # ------------------ Regression loss ------------------
        box_preds_pos = box_preds.view(-1, 4)[fg_masks]
        box_targets_pos = gt_bbox_targets.view(-1, 4)[fg_masks]
        bbox_weight = gt_score_targets[fg_masks].sum(-1)
        loss_box = self.loss_bboxes(box_preds_pos, box_targets_pos, bbox_weight)
        loss_box = loss_box.sum() / num_fgs

        # ------------------ Distribution focal loss  ------------------
        ## process anchors
        anchors = torch.cat(outputs['anchors'], dim=0)
        anchors = anchors[None].repeat(bs, 1, 1).view(-1, 2)
        ## process stride tensors
        strides = torch.cat(outputs['stride_tensor'], dim=0)
        strides = strides.unsqueeze(0).repeat(bs, 1, 1).view(-1, 1)
        ## fg preds
        reg_preds_pos = reg_preds.view(-1, 4*self.cfg['reg_max'])[fg_masks]
        anchors_pos = anchors[fg_masks]
        strides_pos = strides[fg_masks]
        ## compute dfl
        loss_dfl = self.loss_dfl(reg_preds_pos, box_targets_pos, anchors_pos, strides_pos, bbox_weight)
        loss_dfl = loss_dfl.sum() / num_fgs

        # total loss
        if not self.use_dfl:
            losses = loss_cls * self.loss_cls_weight + loss_box * self.loss_box_weight
            loss_dict = dict(
                    loss_cls = loss_cls,
                    loss_box = loss_box,
                    losses = losses
            )
        else:
            losses = loss_cls * self.loss_cls_weight + loss_box * self.loss_box_weight + loss_dfl * self.loss_dfl_weight
            loss_dict = dict(
                    loss_cls = loss_cls,
                    loss_box = loss_box,
                    loss_dfl = loss_dfl,
                    losses = losses
            )

        return loss_dict
    

class ClassificationLoss(nn.Module):
    def __init__(self, cfg, reduction='none'):
        super(ClassificationLoss, self).__init__()
        self.cfg = cfg
        self.reduction = reduction
        # For VFL
        self.alpha = 0.75
        self.gamma = 2.0


    def binary_cross_entropy(self, pred_logits, gt_score):
        loss = F.binary_cross_entropy_with_logits(
            pred_logits.float(), gt_score.float(), reduction='none')

        if self.reduction == 'sum':
            loss = loss.sum()
        elif self.reduction == 'mean':
            loss = loss.mean()

        return loss


    def forward(self, pred_logits, gt_score):
        if self.cfg['cls_loss'] == 'bce':
            return self.binary_cross_entropy(pred_logits, gt_score)


class RegressionLoss(nn.Module):
    def __init__(self, num_classes, reg_max, use_dfl):
        super(RegressionLoss, self).__init__()
        self.num_classes = num_classes
        self.reg_max = reg_max
        self.use_dfl = use_dfl


    def df_loss(self, pred_regs, target):
        gt_left = target.to(torch.long)
        gt_right = gt_left + 1
        weight_left = gt_right.to(torch.float) - target
        weight_right = 1 - weight_left
        # loss left
        loss_left = F.cross_entropy(
            pred_regs.view(-1, self.reg_max + 1),
            gt_left.view(-1),
            reduction='none').view(gt_left.shape) * weight_left
        # loss right
        loss_right = F.cross_entropy(
            pred_regs.view(-1, self.reg_max + 1),
            gt_right.view(-1),
            reduction='none').view(gt_left.shape) * weight_right

        loss = (loss_left + loss_right).mean(-1, keepdim=True)
        
        return loss


    def forward(self, pred_regs, pred_boxs, anchors, gt_boxs, bbox_weight, fg_masks, strides):
        """
        Input:
            pred_regs: (Tensor) [BM, 4*(reg_max + 1)]
            pred_boxs: (Tensor) [BM, 4]
            anchors: (Tensor) [BM, 2]
            gt_boxs: (Tensor) [BM, 4]
            bbox_weight: (Tensor) [BM, 1]
            fg_masks: (Tensor) [BM,]
            strides: (Tensor) [BM, 1]
        """
        # select positive samples mask
        num_pos = fg_masks.sum()

        if num_pos > 0:
            pred_boxs_pos = pred_boxs[fg_masks]
            gt_boxs_pos = gt_boxs[fg_masks]

            # iou loss
            ious = bbox_iou(pred_boxs_pos,
                            gt_boxs_pos,
                            xywh=False,
                            CIoU=True)
            loss_iou = (1.0 - ious) * bbox_weight
               
            # dfl loss
            if self.use_dfl:
                pred_regs_pos = pred_regs[fg_masks]
                gt_boxs_s = gt_boxs / strides
                anchors_s = anchors / strides
                gt_ltrb_s = bbox2dist(anchors_s, gt_boxs_s, self.reg_max)
                gt_ltrb_s_pos = gt_ltrb_s[fg_masks]
                loss_dfl = self.df_loss(pred_regs_pos, gt_ltrb_s_pos)
                loss_dfl *= bbox_weight
            else:
                loss_dfl = pred_regs.sum() * 0.

        else:
            loss_iou = pred_regs.sum() * 0.
            loss_dfl = pred_regs.sum() * 0.

        return loss_iou, loss_dfl


def build_criterion(cfg, device, num_classes):
    criterion = Criterion(
        cfg=cfg,
        device=device,
        num_classes=num_classes
        )

    return criterion


if __name__ == "__main__":
    pass
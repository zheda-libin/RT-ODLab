import math
import warnings
import numpy as np
import torch
import torch.nn as nn


def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    """Copy from timm"""
    with torch.no_grad():
        """Copy from timm"""
        def norm_cdf(x):
            return (1. + math.erf(x / math.sqrt(2.))) / 2.

        if (mean < a - 2 * std) or (mean > b + 2 * std):
            warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                        "The distribution of values may be incorrect.",
                        stacklevel=2)

        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        tensor.uniform_(2 * l - 1, 2 * u - 1)
        tensor.erfinv_()

        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        tensor.clamp_(min=a, max=b)

        return tensor

def box_xyxy_to_cxcywh(x):
    x0, y0, x1, y1 = x.unbind(-1)
    b = [(x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0), (y1 - y0)]
    
    return torch.stack(b, dim=-1)

def delta2bbox(proposals,
               deltas,
               max_shape=None,
               wh_ratio_clip=16 / 1000,
               clip_border=True,
               add_ctr_clamp=False,
               ctr_clamp=32):

    dxy = deltas[..., :2]
    dwh = deltas[..., 2:]

    # Compute width/height of each roi
    pxy = proposals[..., :2]
    pwh = proposals[..., 2:]

    dxy_wh = pwh * dxy
    wh_ratio_clip = torch.as_tensor(wh_ratio_clip)
    max_ratio = torch.abs(torch.log(wh_ratio_clip)).item()
    
    if add_ctr_clamp:
        dxy_wh = torch.clamp(dxy_wh, max=ctr_clamp, min=-ctr_clamp)
        dwh = torch.clamp(dwh, max=max_ratio)
    else:
        dwh = dwh.clamp(min=-max_ratio, max=max_ratio)

    gxy = pxy + dxy_wh
    gwh = pwh * dwh.exp()
    x1y1 = gxy - (gwh * 0.5)
    x2y2 = gxy + (gwh * 0.5)
    bboxes = torch.cat([x1y1, x2y2], dim=-1)
    if clip_border and max_shape is not None:
        bboxes[..., 0::2].clamp_(min=0).clamp_(max=max_shape[1])
        bboxes[..., 1::2].clamp_(min=0).clamp_(max=max_shape[0])

    return bboxes


# ---------------------------- NMS ----------------------------
## basic NMS
def nms(bboxes, scores, nms_thresh):
    """"Pure Python NMS."""
    x1 = bboxes[:, 0]  #xmin
    y1 = bboxes[:, 1]  #ymin
    x2 = bboxes[:, 2]  #xmax
    y2 = bboxes[:, 3]  #ymax

    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]

    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        # compute iou
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])

        w = np.maximum(1e-10, xx2 - xx1)
        h = np.maximum(1e-10, yy2 - yy1)
        inter = w * h

        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-14)
        #reserve all the boundingbox whose ovr less than thresh
        inds = np.where(iou <= nms_thresh)[0]
        order = order[inds + 1]

    return keep

## class-agnostic NMS 
def multiclass_nms_class_agnostic(scores, labels, bboxes, nms_thresh):
    # nms
    keep = nms(bboxes, scores, nms_thresh)
    scores = scores[keep]
    labels = labels[keep]
    bboxes = bboxes[keep]

    return scores, labels, bboxes

## class-aware NMS 
def multiclass_nms_class_aware(scores, labels, bboxes, nms_thresh, num_classes):
    # nms
    keep = np.zeros(len(bboxes), dtype=np.int32)
    for i in range(num_classes):
        inds = np.where(labels == i)[0]
        if len(inds) == 0:
            continue
        c_bboxes = bboxes[inds]
        c_scores = scores[inds]
        c_keep = nms(c_bboxes, c_scores, nms_thresh)
        keep[inds[c_keep]] = 1
    keep = np.where(keep > 0)
    scores = scores[keep]
    labels = labels[keep]
    bboxes = bboxes[keep]

    return scores, labels, bboxes

## multi-class NMS 
def multiclass_nms(scores, labels, bboxes, nms_thresh, num_classes, class_agnostic=False):
    if class_agnostic:
        return multiclass_nms_class_agnostic(scores, labels, bboxes, nms_thresh)
    else:
        return multiclass_nms_class_aware(scores, labels, bboxes, nms_thresh, num_classes)


# ----------------- Customed NormLayer Ops -----------------
class FrozenBatchNorm2d(torch.nn.Module):
    def __init__(self, n):
        super(FrozenBatchNorm2d, self).__init__()
        self.register_buffer("weight", torch.ones(n))
        self.register_buffer("bias", torch.zeros(n))
        self.register_buffer("running_mean", torch.zeros(n))
        self.register_buffer("running_var", torch.ones(n))

    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        num_batches_tracked_key = prefix + 'num_batches_tracked'
        if num_batches_tracked_key in state_dict:
            del state_dict[num_batches_tracked_key]

        super(FrozenBatchNorm2d, self)._load_from_state_dict(
            state_dict, prefix, local_metadata, strict,
            missing_keys, unexpected_keys, error_msgs)

    def forward(self, x):
        # move reshapes to the beginning
        # to make it fuser-friendly
        w = self.weight.reshape(1, -1, 1, 1)
        b = self.bias.reshape(1, -1, 1, 1)
        rv = self.running_var.reshape(1, -1, 1, 1)
        rm = self.running_mean.reshape(1, -1, 1, 1)
        eps = 1e-5
        scale = w * (rv + eps).rsqrt()
        bias = b - rm * scale
        return x * scale + bias

class LayerNorm2D(nn.Module):
    def __init__(self, normalized_shape, norm_layer=nn.LayerNorm):
        super().__init__()
        self.ln = norm_layer(normalized_shape) if norm_layer is not None else nn.Identity()

    def forward(self, x):
        """
        x: N C H W
        """
        x = x.permute(0, 2, 3, 1)
        x = self.ln(x)
        x = x.permute(0, 3, 1, 2)
        return x


# ----------------- Basic CNN Ops -----------------
def get_conv2d(c1, c2, k, p, s, g, bias=False):
    conv = nn.Conv2d(c1, c2, k, stride=s, padding=p, groups=g, bias=bias)

    return conv

def get_activation(act_type=None):
    if act_type == 'relu':
        return nn.ReLU(inplace=True)
    elif act_type == 'lrelu':
        return nn.LeakyReLU(0.1, inplace=True)
    elif act_type == 'mish':
        return nn.Mish(inplace=True)
    elif act_type == 'silu':
        return nn.SiLU(inplace=True)
    elif act_type == 'gelu':
        return nn.GELU()
    elif act_type is None:
        return nn.Identity()
    else:
        raise NotImplementedError
        
def get_norm(norm_type, dim):
    if norm_type == 'BN':
        return nn.BatchNorm2d(dim)
    elif norm_type == 'GN':
        return nn.GroupNorm(num_groups=32, num_channels=dim)
    elif norm_type is None:
        return nn.Identity()
    else:
        raise NotImplementedError

class BasicConv(nn.Module):
    def __init__(self, 
                 in_dim,                   # in channels
                 out_dim,                  # out channels 
                 kernel_size=1,            # kernel size 
                 padding=0,                # padding
                 stride=1,                 # padding
                 act_type  :str = 'lrelu', # activation
                 norm_type :str = 'BN',    # normalization
                ):
        super(BasicConv, self).__init__()
        add_bias = False if norm_type else True
        self.conv = get_conv2d(in_dim, out_dim, k=kernel_size, p=padding, s=stride, g=1, bias=add_bias)
        self.norm = get_norm(norm_type, out_dim)
        self.act  = get_activation(act_type)

    def forward(self, x):
        return self.act(self.norm(self.conv(x)))

class UpSampleWrapper(nn.Module):
    """Upsample last feat map to specific stride."""
    def __init__(self, in_dim, upsample_factor):
        super(UpSampleWrapper, self).__init__()
        # ---------- Basic parameters ----------
        self.upsample_factor = upsample_factor

        # ---------- Network parameters ----------
        if upsample_factor == 1:
            self.upsample = nn.Identity()
        else:
            scale = int(math.log2(upsample_factor))
            dim = in_dim
            layers = []
            for _ in range(scale-1):
                layers += [
                    nn.ConvTranspose2d(dim, dim, kernel_size=2, stride=2),
                    LayerNorm2D(dim),
                    nn.GELU()
                ]
            layers += [nn.ConvTranspose2d(dim, dim, kernel_size=2, stride=2)]
            self.upsample = nn.Sequential(*layers)
            self.out_dim = dim

    def forward(self, x):
        x = self.upsample(x)

        return x


# ----------------- MLP modules -----------------
class MLP(nn.Module):
    def __init__(self, in_dim, hidden_dim, out_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([in_dim] + h, h + [out_dim]))

    def forward(self, x):
        for i, layer in enumerate(self.layers):
            x = nn.functional.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x

class FFN(nn.Module):
    def __init__(self, d_model=256, mlp_ratio=4.0, dropout=0., act_type='relu', pre_norm=False):
        super().__init__()
        # ----------- Basic parameters -----------
        self.pre_norm = pre_norm
        self.fpn_dim = round(d_model * mlp_ratio)
        # ----------- Network parameters -----------
        self.linear1 = nn.Linear(d_model, self.fpn_dim)
        self.activation = get_activation(act_type)
        self.dropout2 = nn.Dropout(dropout)
        self.linear2 = nn.Linear(self.fpn_dim, d_model)
        self.dropout3 = nn.Dropout(dropout)
        self.norm = nn.LayerNorm(d_model)

    def forward(self, src):
        if self.pre_norm:
            src = self.norm(src)
            src2 = self.linear2(self.dropout2(self.activation(self.linear1(src))))
            src = src + self.dropout3(src2)
        else:
            src2 = self.linear2(self.dropout2(self.activation(self.linear1(src))))
            src = src + self.dropout3(src2)
            src = self.norm(src)
        
        return src
    

# ----------------- Attention Ops -----------------
class GlobalCrossAttention(nn.Module):
    def __init__(
        self,
        dim            :int   = 256,
        num_heads      :int   = 8,
        qkv_bias       :bool  = True,
        qk_scale       :float = None,
        attn_drop      :float = 0.0,
        proj_drop      :float = 0.0,
        rpe_hidden_dim :int   = 512,
        feature_stride :int   = 16,
    ):
        super().__init__()
        # --------- Basic parameters ---------
        self.dim = dim
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5
        self.feature_stride = feature_stride

        # --------- Network parameters ---------
        self.cpb_mlp1 = self.build_cpb_mlp(2, rpe_hidden_dim, num_heads)
        self.cpb_mlp2 = self.build_cpb_mlp(2, rpe_hidden_dim, num_heads)
        self.q = nn.Linear(dim, dim, bias=qkv_bias)
        self.k = nn.Linear(dim, dim, bias=qkv_bias)
        self.v = nn.Linear(dim, dim, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.softmax = nn.Softmax(dim=-1)

    def build_cpb_mlp(self, in_dim, hidden_dim, out_dim):
        cpb_mlp = nn.Sequential(nn.Linear(in_dim, hidden_dim, bias=True),
                                nn.ReLU(inplace=True),
                                nn.Linear(hidden_dim, out_dim, bias=False))
        return cpb_mlp

    def forward(
        self,
        query,
        reference_points,
        k_input_flatten,
        v_input_flatten,
        input_spatial_shapes,
        input_padding_mask=None,
    ):
        assert input_spatial_shapes.size(0) == 1, 'This is designed for single-scale decoder.'
        h, w = input_spatial_shapes[0]
        stride = self.feature_stride

        ref_pts = torch.cat([
            reference_points[:, :, :, :2] - reference_points[:, :, :, 2:] / 2,
            reference_points[:, :, :, :2] + reference_points[:, :, :, 2:] / 2,
        ], dim=-1)  # B, nQ, 1, 4

        pos_x = torch.linspace(0.5, w - 0.5, w, dtype=torch.float32, device=w.device)[None, None, :, None] * stride  # 1, 1, w, 1
        pos_y = torch.linspace(0.5, h - 0.5, h, dtype=torch.float32, device=h.device)[None, None, :, None] * stride  # 1, 1, h, 1

        delta_x = ref_pts[..., 0::2] - pos_x  # B, nQ, w, 2
        delta_y = ref_pts[..., 1::2] - pos_y  # B, nQ, h, 2

        rpe_x, rpe_y = self.cpb_mlp1(delta_x), self.cpb_mlp2(delta_y)  # B, nQ, w/h, nheads
        rpe = (rpe_x[:, :, None] + rpe_y[:, :, :, None]).flatten(2, 3) # B, nQ, h, w, nheads ->  B, nQ, h*w, nheads
        rpe = rpe.permute(0, 3, 1, 2)

        B_, N, C = k_input_flatten.shape
        k = self.k(k_input_flatten).reshape(B_, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        v = self.v(v_input_flatten).reshape(B_, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        B_, N, C = query.shape
        q = self.q(query).reshape(B_, N, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3)
        q = q * self.scale

        attn = q @ k.transpose(-2, -1)
        attn += rpe
        if input_padding_mask is not None:
            attn += input_padding_mask[:, None, None] * -100

        fmin, fmax = torch.finfo(attn.dtype).min, torch.finfo(attn.dtype).max
        torch.clip_(attn, min=fmin, max=fmax)

        attn = self.softmax(attn)
        attn = self.attn_drop(attn)
        x = attn @ v

        x = x.transpose(1, 2).reshape(B_, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)

        return x

import torch
from torch.nn import functional as F

from detectron2.layers import cat
from detectron2.modeling.poolers import ROIPooler


class MaskAssembly(object):
    def __init__(self, cfg):
        self.pooler_resolution = cfg.MODEL.LEAFMASK.BOTTOM_RESOLUTION
        sampling_ratio         = cfg.MODEL.LEAFMASK.POOLER_SAMPLING_RATIO
        pooler_type            = cfg.MODEL.LEAFMASK.POOLER_TYPE
        pooler_scales          = cfg.MODEL.LEAFMASK.POOLER_SCALES
        self.attn_size         = cfg.MODEL.LEAFMASK.ATTN_SIZE
        self.top_interp        = cfg.MODEL.LEAFMASK.TOP_INTERP
        self.pooler = ROIPooler(
            output_size=self.pooler_resolution,
            scales=pooler_scales,
            sampling_ratio=sampling_ratio,
            pooler_type=pooler_type,
            canonical_level=2)

    # 该方法的功能类似于在类中重载 () 运算符，使得类实例对象可以像调用普通函数那样，以“对象名()”的形式使用。
    def __call__(self, bases, proposals, gt_instances):
        if gt_instances is not None:
            dense_info = proposals["instances"]
            attns = dense_info.top_feats
            pos_inds = dense_info.pos_inds
            if pos_inds.numel() == 0:
                return None, {"loss_mask": sum([x.sum() * 0 for x in attns]) + bases[0].sum() * 0}
            gt_inds = dense_info.gt_inds
            rois = self.pooler(bases, [x.gt_boxes for x in gt_instances])
            rois = rois[gt_inds]
            pred_mask_logits = self.merge(rois, attns)
            gt_masks = []
            for instances_per_image in gt_instances:
                if len(instances_per_image.gt_boxes.tensor) == 0:
                    continue
                gt_mask_per_image = instances_per_image.gt_masks.crop_and_resize(
                    instances_per_image.gt_boxes.tensor, self.pooler_resolution
                ).to(device=pred_mask_logits.device)
                gt_masks.append(gt_mask_per_image)
            gt_masks = cat(gt_masks, dim=0)
            gt_masks = gt_masks[gt_inds]
            N = gt_masks.size(0)
            gt_masks = gt_masks.view(N, -1)
            gt_ctr = dense_info.gt_ctrs
            loss_denorm = proposals["loss_denorm"]
            mask_losses = F.binary_cross_entropy_with_logits(
                pred_mask_logits, gt_masks.to(dtype=torch.float32), reduction="none")
            mask_loss = ((mask_losses.mean(dim=-1) * gt_ctr).sum() / loss_denorm)

            return None, {"loss_mask": mask_loss}, pred_mask_logits

        else:
            total_instances = sum([len(x) for x in proposals])
            if total_instances == 0:
                for box in proposals:
                    box.pred_masks = box.pred_classes.view(-1, 1, self.pooler_resolution, self.pooler_resolution)
                return proposals, {}, None
            rois = self.pooler(bases, [x.pred_boxes for x in proposals])
            attns = cat([x.top_feat for x in proposals], dim=0)
            pred_mask_logits = self.merge(rois, attns)
            pred_mask_logits = pred_mask_logits.view(-1, 1, self.pooler_resolution, self.pooler_resolution)
            start_ind = 0
            for box in proposals:
                end_ind = start_ind + len(box)
                box.pred_masks = pred_mask_logits[start_ind:end_ind]
                start_ind = end_ind

            return proposals, {}, pred_mask_logits


    def merge(self, rois, coeffs, location_to_inds=None):
        if location_to_inds is not None:
            rois = rois[location_to_inds]
        N, B, H, W = rois.size()
        coeffs = coeffs.view(N, -1, self.attn_size, self.attn_size)
        coeffs = F.interpolate(coeffs, (H, W), mode=self.top_interp).softmax(dim=1)
        masks_preds = (rois * coeffs).sum(dim=1)

        return masks_preds.view(N, -1)

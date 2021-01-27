import os
import cv2
import torch
import numpy as np
from types import SimpleNamespace
from .fan import FAN


class FANPredictor(object):
    def __init__(self, device='cuda:0', model=None, config=None):
        self.device = device
        if model is None:
            model = FANPredictor.get_model()
        if config is None:
            config = FANPredictor.create_config()
        self.config = SimpleNamespace(**model.config.__dict__, **config.__dict__)
        self.net = FAN(config=self.config).to(self.device)
        self.net.load_state_dict(torch.load(model.weights, map_location=self.device))
        self.net.eval()

    @staticmethod
    def get_model(name='2dfan4'):
        name = name.lower()
        if name == '2dfan4':
            return SimpleNamespace(weights=os.path.join(os.path.dirname(__file__), 'weights', '2dfan4.pth'),
                                   config=SimpleNamespace(crop_ratio=0.55, input_size=256, num_modules=4,
                                                          hg_num_features=256, hg_depth=4, hg_use_avg_pool=True))
        elif name == '2dfan2':
            return SimpleNamespace(weights=os.path.join(os.path.dirname(__file__), 'weights', '2dfan2.pth'),
                                   config=SimpleNamespace(crop_ratio=0.55, input_size=256, num_modules=2,
                                                          hg_num_features=256, hg_depth=4, hg_use_avg_pool=False))
        else:
            raise ValueError('name must be set to fan4')

    @staticmethod
    def create_config(gamma=1.0, radius=0.1, return_features=False):
        return SimpleNamespace(gamma=gamma, radius=radius, return_features=return_features)

    @torch.no_grad()
    def __call__(self, image, face_boxes, rgb=True):
        if face_boxes.shape[0] > 0:
            if not rgb:
                image = image[..., ::-1]

            # Crop the faces
            face_patches = []
            centres = (face_boxes[:, [0, 1]] + face_boxes[:, [2, 3]]) / 2.0
            face_sizes = (face_boxes[:, [3, 2]] - face_boxes[:, [1, 0]]).mean(axis=1)
            enlarged_face_box_sizes = (face_sizes / self.config.crop_ratio)[:, np.newaxis].repeat(2, axis=1)
            enlarged_face_boxes = np.zeros_like(face_boxes[:, :4])
            enlarged_face_boxes[:, :2] = np.round(centres - enlarged_face_box_sizes / 2.0)
            enlarged_face_boxes[:, 2:] = np.round(enlarged_face_boxes[:, :2] + enlarged_face_box_sizes) + 1
            enlarged_face_boxes = enlarged_face_boxes.astype(int)
            outer_bounding_box = np.hstack((enlarged_face_boxes[:, :2].min(axis=0),
                                            enlarged_face_boxes[:, 2:].max(axis=0)))
            pad_widths = np.zeros(shape=(3, 2), dtype=int)
            if outer_bounding_box[0] < 0:
                pad_widths[1][0] = -outer_bounding_box[0]
            if outer_bounding_box[1] < 0:
                pad_widths[0][0] = -outer_bounding_box[1]
            if outer_bounding_box[2] > image.shape[1]:
                pad_widths[1][1] = outer_bounding_box[2] - image.shape[1]
            if outer_bounding_box[3] > image.shape[0]:
                pad_widths[0][1] = outer_bounding_box[3] - image.shape[0]
            if np.any(pad_widths > 0):
                image = np.pad(image, pad_widths)
            for left, top, right, bottom in enlarged_face_boxes:
                left += pad_widths[1][0]
                top += pad_widths[0][0]
                right += pad_widths[1][0]
                bottom += pad_widths[0][0]
                face_patches.append(cv2.resize(image[top: bottom, left: right, :],
                                               (self.config.input_size, self.config.input_size)))
            face_patches = torch.from_numpy(np.array(face_patches).transpose(
                (0, 3, 1, 2)).astype(np.float32)).to(self.device) / 255.0

            # Get heatmaps
            heatmaps = self.net(face_patches).detach()

            # Get landmark coordinates and scores
            landmarks, landmark_scores = self._decode(heatmaps)

            # Rectify landmark coordinates
            hh, hw = heatmaps.size(2), heatmaps.size(3)
            for landmark, (left, top, right, bottom) in zip(landmarks, enlarged_face_boxes):
                landmark[:, 0] = landmark[:, 0] * (right - left) / hw + left
                landmark[:, 1] = landmark[:, 1] * (bottom - top) / hh + top

            return landmarks, landmark_scores
        else:
            return np.empty(shape=(0, 68, 2), dtype=np.float32), np.empty(shape=(0, 68), dtype=np.float32)

    def _decode(self, heatmaps):
        heatmaps = heatmaps.contiguous()
        scores = heatmaps.max(dim=3)[0].max(dim=2)[0].cpu().numpy()

        if (self.config.radius * heatmaps.shape[2] * heatmaps.shape[3] <
                heatmaps.shape[2] ** 2 + heatmaps.shape[3] ** 2):
            # Find peaks in all heatmaps
            m = heatmaps.view(heatmaps.shape[0] * heatmaps.shape[1], -1).argmax(1)
            all_peaks = torch.cat(
                [(m // heatmaps.shape[3]).view(-1, 1), (m % heatmaps.shape[3]).view(-1, 1)], dim=1
            ).reshape((heatmaps.shape[0], heatmaps.shape[1], 1, 1, 2)).repeat(
                1, 1, heatmaps.shape[2], heatmaps.shape[3], 1).float()

            # Apply masks created from the peaks
            all_indices = torch.zeros_like(all_peaks) + torch.stack(
                [torch.arange(0.0, all_peaks.shape[2],
                              device=all_peaks.device).unsqueeze(-1).repeat(1, all_peaks.shape[3]),
                 torch.arange(0.0, all_peaks.shape[3],
                              device=all_peaks.device).unsqueeze(0).repeat(all_peaks.shape[2], 1)], dim=-1)
            heatmaps = heatmaps * ((all_indices - all_peaks).norm(dim=-1) <= self.config.radius *
                                   (heatmaps.shape[2] * heatmaps.shape[3]) ** 0.5).float()

        # Prepare the indices for calculating centroids
        x_indices = (torch.zeros((*heatmaps.shape[:2], heatmaps.shape[3]), device=heatmaps.device) +
                     torch.arange(0.5, heatmaps.shape[3], device=heatmaps.device))
        y_indices = (torch.zeros(heatmaps.shape[:3], device=heatmaps.device) +
                     torch.arange(0.5, heatmaps.shape[2], device=heatmaps.device))

        # Finally, find centroids as landmark locations
        heatmaps = heatmaps.clamp_min(0.0)
        if self.config.gamma != 1.0:
            heatmaps = heatmaps.pow(self.config.gamma)
        m00s = heatmaps.sum(dim=(2, 3))
        xs = heatmaps.sum(dim=2).mul(x_indices).sum(dim=2).div(m00s).cpu().numpy()
        ys = heatmaps.sum(dim=3).mul(y_indices).sum(dim=2).div(m00s).cpu().numpy()

        return np.stack((xs, ys), axis=-1), scores
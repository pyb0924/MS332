import argparse
import json
from pathlib import Path
from validation import validation_binary, validation_multi, validation_all
import numpy as np

import torch
from torch import nn
from torch.optim import Adam
from torch.utils.data import DataLoader
import torch.backends.cudnn as cudnn
import torch.backends.cudnn

from models.models import UNet11, LinkNet34, UNet, UNet16, AlbuNet, TDSNet
from loss import LossBinary, LossMulti, LossAll
from dataset import RoboticsDataset
import utils
import sys
from prepare_train_val import get_split

from albumentations import (
    HorizontalFlip,
    VerticalFlip,
    Normalize,
    Compose,
    PadIfNeeded,
    RandomCrop,
    CenterCrop
)

moddel_list = {'UNet11': UNet11,
               'UNet16': UNet16,
               'UNet': UNet,
               'AlbuNet': AlbuNet,
               'LinkNet34': LinkNet34,
               'TDSNet': TDSNet
               }


def main():
    parser = argparse.ArgumentParser()
    arg = parser.add_argument
    arg('--jaccard-weight', default=0.3, type=float)
    arg('--device-ids', type=str, default='0', help='For example 0,1 to run on two GPUs')
    arg('--fold', type=int, help='fold', default=1)
    arg('--root', default='runs/debug', help='checkpoint root')
    arg('--batch-size', type=int, default=8)
    arg('--n-epochs', type=int, default=5)
    arg('--lr', type=float, default=0.0001)
    arg('--workers', type=int, default=1)
    arg('--train_crop_height', type=int, default=512)
    arg('--train_crop_width', type=int, default=640)
    arg('--val_crop_height', type=int, default=512)
    arg('--val_crop_width', type=int, default=640)
    arg('--type', type=str, default='binary', choices=['binary', 'parts', 'instruments', 'all'])
    arg('--model', type=str, default='UNet11', choices=moddel_list.keys())
    arg('--ends-flag', type=int, default=0)
    arg('--alpha', default=0.3, type=float)
    arg('--beta', default=0.2, type=float)
    arg('--gamma', default=0.5, type=float)

    args = parser.parse_args()

    root = Path(args.root)
    root.mkdir(exist_ok=True, parents=True)

    if not utils.check_crop_size(args.train_crop_height, args.train_crop_width):
        print('Input image sizes should be divisible by 32, but train '
              'crop sizes ({train_crop_height} and {train_crop_width}) '
              'are not.'.format(train_crop_height=args.train_crop_height, train_crop_width=args.train_crop_width))
        sys.exit(0)

    if not utils.check_crop_size(args.val_crop_height, args.val_crop_width):
        print('Input image sizes should be divisible by 32, but validation '
              'crop sizes ({val_crop_height} and {val_crop_width}) '
              'are not.'.format(val_crop_height=args.val_crop_height, val_crop_width=args.val_crop_width))
        sys.exit(0)

    if args.type == 'parts':
        num_classes = 4
    elif args.type == 'binary':
        num_classes = 1
    else:
        num_classes = 8

    if args.model == 'UNet':
        model = UNet(num_classes=num_classes)
    elif args.type == 'all':
        model = TDSNet(num_classes=num_classes)
    else:
        model_name = moddel_list[args.model]
        model = model_name(num_classes=num_classes, pretrained=True)

    if torch.cuda.is_available():
        if args.device_ids:
            device_ids = list(map(int, args.device_ids.split(',')))
        else:
            device_ids = None
        model = nn.DataParallel(model, device_ids=device_ids).cuda()
    else:
        raise SystemError('GPU device not found')

    if args.type == 'binary':
        loss = LossBinary(jaccard_weight=args.jaccard_weight)
    elif args.type == 'parts':
        loss = LossMulti(num_classes=num_classes, jaccard_weight=args.jaccard_weight)
    elif args.type == 'instruments':
        class_weight = np.array([0.1, 0.05, 0.05, 0.2, 0.2, 0.2, 0.1, 0.1], dtype=np.float32)
        loss = LossMulti(num_classes=num_classes, jaccard_weight=args.jaccard_weight, class_weights=class_weight)
    elif args.type == 'all':
        class_weight = None
        loss = LossAll(args.alpha, args.beta, args.gamma, num_classes=num_classes, jaccard_weight=args.jaccard_weight,
                       class_weights=class_weight)

    cudnn.benchmark = True

    def make_loader(file_names, shuffle=False, transform=None, problem_type='binary', batch_size=1):
        return DataLoader(
            dataset=RoboticsDataset(file_names, transform=transform, problem_type=problem_type),
            shuffle=shuffle,
            num_workers=args.workers,
            batch_size=batch_size,
            pin_memory=torch.cuda.is_available()
        )

    train_file_names, val_file_names = get_split(args.fold)

    print('num train = {}, num_val = {}, model={}, type={}, fold={}'.format(len(train_file_names), len(val_file_names),
                                                                            args.model, args.type, args.fold))

    def train_transform(p=1):
        return Compose([
            PadIfNeeded(min_height=args.train_crop_height, min_width=args.train_crop_width, p=1),
            RandomCrop(height=args.train_crop_height, width=args.train_crop_width, p=1),
            VerticalFlip(p=0.5),
            HorizontalFlip(p=0.5),
            Normalize(p=1)
        ], p=p)

    def val_transform(p=1):
        return Compose([
            PadIfNeeded(min_height=args.val_crop_height, min_width=args.val_crop_width, p=1),
            CenterCrop(height=args.val_crop_height, width=args.val_crop_width, p=1),
            Normalize(p=1)
        ], p=p)

    train_loader = make_loader(train_file_names, shuffle=True, transform=train_transform(p=1), problem_type=args.type,
                               batch_size=args.batch_size)
    valid_loader = make_loader(val_file_names, transform=val_transform(p=1), problem_type=args.type,
                               batch_size=len(device_ids))

    root.joinpath('params.json').write_text(
        json.dumps(vars(args), indent=True, sort_keys=True))

    if args.type == 'binary':
        valid = validation_binary
    elif args.type == 'all':
        valid = validation_all
    else:
        valid = validation_multi

    utils.train(
        init_optimizer=lambda lr: Adam(model.parameters(), lr=lr),
        args=args,
        model=model,
        criterion=loss,
        train_loader=train_loader,
        valid_loader=valid_loader,
        validation=valid,
        fold=args.fold,
        num_classes=num_classes
    )


if __name__ == '__main__':
    main()

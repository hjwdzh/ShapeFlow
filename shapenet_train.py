"""Training script shapenet deformation space experiment.
"""
import argparse
import json
import os
import glob
import numpy as np
from collections import defaultdict
import time

import deepdeform.utils.train_utils as utils
from deepdeform.layers.pointnet_layer import PointNetEncoder
from deepdeform.layers.chamfer_layer import ChamferDistKDTree
from deepdeform.layers.deformation_layer import NeuralFlowDeformer
from shapenet_dataloader import ShapeNetVertexSampler, ShapeNetMeshLoader

import torch
import torch.nn.functional as F
import torch.optim as optim
import torch.nn as nn
from torch.utils.data import DataLoader, RandomSampler
from torch.utils.tensorboard import SummaryWriter

np.set_printoptions(precision=4)


# Various choices for losses and optimizers
LOSSES = {
    'l1': F.l1_loss,
    'l2': F.mse_loss,
    'huber': F.smooth_l1_loss,
}

OPTIMIZERS = {
    'sgd': optim.SGD,
    'adam': optim.Adam,
    'adadelta': optim.Adadelta,
    'adagrad': optim.Adagrad,
    'rmsprop': optim.RMSprop,
}


def train_or_eval(mode, args, encoder, deformer, chamfer_dist, dataloader, epoch, 
                  global_step, device, logger, writer, optimizer, vis_loader=None):
    """Training / Eval function."""
    modes = ["train", "eval"]
    if not mode in modes:
        raise ValueError(f"mode ({mode}) must be one of {modes}.")
    if mode == 'train':
        encoder.train()
        deformer.train()
    else:
        encoder.eval()
        deformer.eval()
    tot_loss = 0
    count = 0
    criterion = LOSSES[args.loss_type]
    
    with torch.set_grad_enabled(mode == 'train'):
        toc = time.time()
        
        for batch_idx, data_tensors in enumerate(dataloader):
            tic = time.time()
            # send tensors to device
            data_tensors = [t.to(device) for t in data_tensors]
            source_pts, target_pts = data_tensors
            bs = len(source_pts)
            optimizer.zero_grad()

            source_latents = encoder(source_pts)
            target_latents = encoder(target_pts)

            src_to_tar = deformer(source_pts[..., :3], source_latents, target_latents)
            tar_to_src = deformer(target_pts[..., :3], target_latents, source_latents)

            # symmetric pair of matching losses
            _, _, src_to_tar_dist = chamfer_dist(src_to_tar, target_pts[..., :3])
            _, _, tar_to_src_dist = chamfer_dist(tar_to_src, source_pts[..., :3])

            loss_src_to_tar = criterion(src_to_tar_dist, torch.zeros_like(src_to_tar_dist))
            loss_tar_to_src = criterion(tar_to_src_dist, torch.zeros_like(tar_to_src_dist))

            loss = loss_src_to_tar + loss_tar_to_src
            if mode == 'train': 
                loss.backward()
                
                # gradient clipping
                torch.nn.utils.clip_grad_value_(encoder.module.parameters(), args.clip_grad)
                torch.nn.utils.clip_grad_value_(deformer.module.parameters(), args.clip_grad)
                
                optimizer.step()

            tot_loss += loss.item()
            count += source_pts.size()[0]
            if batch_idx % args.log_interval == 0:
                # logger log
                logger.info(
                    "{} Epoch: {} [{}/{} ({:.0f}%)]\tLoss Sum: {:.6f}\t"
                    "Loss s2t: {:.6f}\tLoss t2s: {:.6f}\t"
                    "DataTime: {:.4f}\tComputeTime: {:.4f}".format(
                        mode, epoch, batch_idx * bs, len(dataloader) * bs,
                        100. * batch_idx / len(dataloader), loss.item(),
                        loss_src_to_tar.item(), loss_tar_to_src.item(),
                        tic - toc, time.time() - tic))
                # tensorboard log
                writer.add_scalar(f'{mode}/loss_sum', loss, global_step=int(global_step))
                writer.add_scalar(f'{mode}/loss_s2t', loss_src_to_tar, global_step=int(global_step))
                writer.add_scalar(f'{mode}/loss_t2s', loss_tar_to_src, global_step=int(global_step))
            
            if mode == 'train': global_step += 1
            toc = time.time()
    tot_loss /= count
    
    # visualize a few deformations in tensorboard
    if args.vis_mesh and (vis_loader is not None) and (mode == 'eval'):
        with torch.set_grad_enabled(False):
            n_meshes = 2
            idx_choices = np.random.permutation(len(vis_loader))[:n_meshes]
            for ind, idx in enumerate(idx_choices):
                data_tensors = vis_loader[idx] 
                data_tensors = [t.unsqueeze(0).to(device) for t in data_tensors]
                vi, fi, vj, fj = data_tensors
                lat_i = encoder(vi)
                lat_j = encoder(vj)
                vi_j = deformer(vi[..., :3], lat_i, lat_j)
                vj_i = deformer(vj[..., :3], lat_j, lat_i)
                accu_i, _, _ = chamfer_dist(vi_j, vj)  # [1, m]
                accu_j, _, _ = chamfer_dist(vj_i, vi)  # [1, n]
    
                # find the max dist between pairs of original shapes for normalizing colors
                chamfer_dist.set_reduction_method('max')
                _, _, max_dist = chamfer_dist(vi, vj)  # [1,]
                chamfer_dist.set_reduction_method('mean')
                
                # normalize the accuracies wrt. the distance between src and tgt meshes
                ci = utils.colorize_scalar_tensors(accu_i / max_dist, 
                                                   vmin=0., vmax=1., cmap='coolwarm')
                cj = utils.colorize_scalar_tensors(accu_j / max_dist, 
                                                   vmin=0., vmax=1., cmap='coolwarm')
                ci = (ci * 255.).int()
                cj = (cj * 255.).int()
                
                # add colorized mesh to tensorboard
                writer.add_mesh(f'samp{ind}/src', vertices=vi, faces=fi, global_step=int(global_step))
                writer.add_mesh(f'samp{ind}/tar', vertices=vj, faces=fj, global_step=int(global_step))
                writer.add_mesh(f'samp{ind}/src_to_tar', vertices=vi_j, faces=fi, colors=ci, 
                                global_step=int(global_step))
                writer.add_mesh(f'samp{ind}/tar_to_src', vertices=vj_i, faces=fj, colors=cj, 
                                global_step=int(global_step))
    
    return tot_loss


def get_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="ShapeNet Deformation Space")
    
    parser.add_argument("--batch_size_per_gpu", type=int, default=16, metavar="N",
                        help="input batch size for training (default: 10)")
    parser.add_argument("--epochs", type=int, default=100, metavar="N",
                        help="number of epochs to train (default: 100)")
    parser.add_argument("--pseudo_train_epoch_size", type=int, default=2048, metavar="N",
                        help="number of samples in an pseudo-epoch. (default: 2048)")
    parser.add_argument("--pseudo_eval_epoch_size", type=int, default=128, metavar="N",
                        help="number of samples in an pseudo-epoch. (default: 128)")
    parser.add_argument("--lr", type=float, default=1e-3, metavar="R",
                        help="learning rate (default: 0.001)")
    parser.add_argument("--no_cuda", action="store_true", default=False,
                        help="disables CUDA training")
    parser.add_argument("--seed", type=int, default=1, metavar="S",
                        help="random seed (default: 1)")
    parser.add_argument("--data_root", type=str, default="data/shapenet_watertight",
                        help="path to data folder root (default: data/shapenet_watertight)")
    parser.add_argument("--deformer_arch", type=str, choices=["imnet", "vanilla"], default="imnet",
                        help="deformer architecture. (default: imnet)")
    parser.add_argument("--log_interval", type=int, default=10, metavar="N",
                        help="how many batches to wait before logging training status")
    parser.add_argument("--log_dir", type=str, required=True, help="log directory for run")
    parser.add_argument("--optim", type=str, default="adam", choices=list(OPTIMIZERS.keys()))
    parser.add_argument("--loss_type", type=str, default="l2", choices=list(LOSSES.keys()))
    parser.add_argument("--resume", type=str, default=None,
                        help="path to checkpoint if resume is needed")
    parser.add_argument("-n", "--nsamples", default=2048, type=int,
                        help="number of sample points to draw per shape.")
    parser.add_argument("--lat_dims", default=64, type=int, help="number of latent dimensions.")
    parser.add_argument("--encoder_nf", default=32, type=int,
                        help="number of base number of feature layers in encoder (pointnet).")
    parser.add_argument("--deformer_nf", default=64, type=int,
                        help="number of base number of feature layers in deformer (imnet).")
    parser.add_argument("--lr_scheduler", dest='lr_scheduler', action='store_true')
    parser.add_argument("--no_lr_scheduler", dest='lr_scheduler', action='store_false')
    parser.set_defaults(lr_scheduler=True)
    parser.add_argument("--normals", dest='normals', action='store_true',
                        help='add normals to input point features fed to the pointnet encoder.')
    parser.add_argument("--no_normals", dest='normals', action='store_false',
                        help='not add normals to input point features fed to the pointnet encoder.')
    parser.set_defaults(normals=True)
    parser.add_argument("--visualize_mesh", dest='vis_mesh', action='store_true',
                        help="visualize deformation for meshes of sample validation data in tensorboard.")
    parser.add_argument("--no_visualize_mesh", dest='vis_mesh', action='store_false',
                        help="no visualize deformation for meshes of sample validation data in tensorboard.")
    parser.set_defaults(vis_mesh=True)
    parser.add_argument("--clip_grad", default=1., type=float,
                        help="clip gradient to this value. large value basically deactivates it.")

    args = parser.parse_args()
    return args


def main():
    args = get_args()

    # adjust batch size based on the number of gpus available
    args.batch_size = int(torch.cuda.device_count()) * args.batch_size_per_gpu
    use_cuda = (not args.no_cuda) and torch.cuda.is_available()
    kwargs = {'num_workers': min(12, args.batch_size), 'pin_memory': True} if use_cuda else {}
    device = torch.device("cuda" if use_cuda else "cpu")
    
    # log and create snapshots
    filenames_to_snapshot = glob.glob("*.py") + glob.glob("*.sh") + glob.glob("layers/*.py")
    utils.snapshot_files(filenames_to_snapshot, args.log_dir)
    logger = utils.get_logger(log_dir=args.log_dir)
    with open(os.path.join(args.log_dir, "params.json"), 'w') as fh:
        json.dump(args.__dict__, fh, indent=2)
    logger.info("%s", repr(args))

    # tensorboard writer
    writer = SummaryWriter(log_dir=os.path.join(args.log_dir, 'tensorboard'))

    # random seed for reproducability
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    # create dataloaders
    trainset = ShapeNetVertexSampler(data_root=args.data_root, split="train", category="chair", 
                                     nsamples=5000, normals=args.normals)
    evalset = ShapeNetVertexSampler(data_root=args.data_root, split="val", category="chair",
                                    nsamples=5000, normals=args.normals)

    train_sampler = RandomSampler(trainset, replacement=True, num_samples=args.pseudo_train_epoch_size)
    eval_sampler = RandomSampler(evalset, replacement=True, num_samples=args.pseudo_eval_epoch_size)

    train_loader = DataLoader(trainset, batch_size=args.batch_size, shuffle=False, drop_last=True,
                              sampler=train_sampler, **kwargs)
    eval_loader = DataLoader(evalset, batch_size=args.batch_size, shuffle=False, drop_last=False,
                             sampler=eval_sampler, **kwargs)
    if args.vis_mesh:
        # for loading full meshes for visualization
        simp_data_root = args.data_root.replace('shapenet_watertight', 'shapenet_simplified')
        vis_loader = ShapeNetMeshLoader(data_root=simp_data_root, split="val", category="chair", 
                                        normals=args.normals)
    else:
        vis_loader = None
        
    # setup model
    in_feat = 6 if args.normals else 3
    encoder = PointNetEncoder(nf=16, in_features=in_feat, out_features=args.lat_dims).to(device)
    deformer = NeuralFlowDeformer(latent_size=args.lat_dims, f_width=args.deformer_nf, s_nlayers=3, 
                                  s_width=16, method='rk4', nonlinearity='leakyrelu', arch='imnet')
    all_model_params = list(deformer.parameters())+list(encoder.parameters())

    optimizer = OPTIMIZERS[args.optim](all_model_params, lr=args.lr)
    start_ep = 0
    global_step = np.zeros(1, dtype=np.uint32)
    tracked_stats = np.inf

    if args.resume:
        resume_dict = torch.load(args.resume)
        start_ep = resume_dict["epoch"]
        global_step = resume_dict["global_step"]
        tracked_stats = resume_dict["tracked_stats"]
        encoder.load_state_dict(resume_dict["encoder_state_dict"])
        deformer.load_state_dict(resume_dict["deformer_state_dict"])
        optimizer.load_state_dict(resume_dict["optim_state_dict"])
        for state in optimizer.state.values():
            for k, v in state.items():
                if isinstance(v, torch.Tensor):
                    state[k] = v.to(device)

    # more threads don't seem to help
    chamfer_dist = ChamferDistKDTree(reduction='mean', njobs=1)
    chamfer_dist.to(device)
    encoder = nn.DataParallel(encoder)
    encoder.to(device)
    deformer = nn.DataParallel(deformer)
    deformer.to(device)

    model_param_count = lambda model: sum(x.numel() for x in model.parameters())
    logger.info(("{}(encoder) + {}(deformer) paramerters in total"
                 .format(model_param_count(encoder), model_param_count(deformer))))

    checkpoint_path = os.path.join(args.log_dir, "checkpoint_latest.pth.tar")

    if args.lr_scheduler:
        scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'min')

    # training loop
    for epoch in range(start_ep + 1, args.epochs + 1):
        loss_train = train_or_eval("train", args, encoder, deformer, chamfer_dist, train_loader, 
                                   epoch, global_step, device, logger, writer, optimizer, None)
        loss_eval = train_or_eval("eval", args, encoder, deformer, chamfer_dist, eval_loader, 
                                  epoch, global_step, device, logger, writer, optimizer, vis_loader)
        if args.lr_scheduler:
            scheduler.step(loss_eval)
        if loss_eval < tracked_stats:
            tracked_stats = loss_eval
            is_best = True
        else:
            is_best = False

        utils.save_checkpoint({
            "epoch": epoch,
            "encoder_state_dict": encoder.module.state_dict(),
            "deformer_state_dict": deformer.module.state_dict(),
            "optim_state_dict": optimizer.state_dict(),
            "tracked_stats": tracked_stats,
            "global_step": global_step,
        }, is_best, epoch, checkpoint_path, "_meshflow", logger)

if __name__ == "__main__":
    main()
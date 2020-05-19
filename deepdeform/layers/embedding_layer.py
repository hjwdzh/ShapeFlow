from scipy.spatial import cKDTree
from deepdeform.layers.chamfer_layer import ChamferDistKDTree
from torch.utils.data import SubsetRandomSampler
from .shared_definition import *


class LatentEmbedder(object):
    """Helper class for embedding new observation in deformation latent space.
    """
    
    def __init__(self, point_dataset, mesh_dataset, deformer):
        """Initialize embedder.
        
        Args:
          point_dataset: instance of FixedPointsCachedDataset
          mesh_dataset: instance of ShapeNetMesh
          deformer: pretrined deformer instance
        """
        self.point_dataset = point_dataset
        self.mesh_dataset = mesh_dataset
        self.deformer = deformer
                    
        self.tree = cKDTree(self.lat_params.clone().detach().cpu().numpy())
            
    @property
    def lat_dims(self):
        return self.lat_params.shape[1]
    
    @property
    def lat_params(self):
        return self.deformer.net.lat_params
    
    @property
    def symm(self):
        return self.deformer.symm_dim is not None
    
    @property
    def device(self):
        return self.lat_params.device
    
    def _padded_verts_from_meshes(self, meshes):
        verts = [vf[0] for vf in meshes]
        faces = [vf[1] for vf in meshes]
        nv = [v.shape[0] for v in verts]
        max_nv = np.max(nv)
        verts_pad = [np.pad(verts[i], ((0, max_nv-nv[i]), (0, 0))) for i in range(len(nv))]
        verts_pad = np.stack(verts_pad, 0)  # [nmesh, max_nv, 3]
        return verts_pad, faces, nv
    
    def _meshes_from_padded_verts(self, verts_pad, faces, nv):
        verts_pad = [v for v in verts_pad]
        verts = [v[:n] for v, n in zip(verts_pad, nv)]
        meshes = list(zip(verts, faces))
        return meshes
                
    def embed(self, input_points, optimizer='adam', lr=1e-3, seed=0,
              niter=20, bs=32, verbose=False, matching="one_way", loss_type='l1',
              topk_finetune=10):
        """Embed inputs points observations into deformation latent space.
        
        Args: 
          input_points: tensor of shape [bs_tar, npoints, 3]
          optimizer: str, optimizer choice. one of sgd, adam, adadelta, adagrad, rmsprop.
          lr: float, learning rate.
          seed: int, random seed.
          niter: int, number of optimization iterations.
          bs: int, batch size.
          verbose: bool, turn on verbose.
          matching: str, matching function. choice of one_way or two_way.
          loss_type: str, loss type. choice of l1, l2, huber.
          topk_finetune: int, topk nearest neighbor to finetune.
        
        Returns:
          embedded_latents: tensor of shape [batch, lat_dims]
        """
        if input_points.shape[0] != 1:
            raise NotImplementedError("Code is not ready for batch size > 1.")
        torch.manual_seed(seed)
        
        # check input validity
        if not matching in ["one_way", "two_way"]:
            raise ValueError(f"matching method must be one of one_way / two_way. Instead entered {matching}")
        if not loss_type in LOSSES.keys():
            raise ValueError(f"loss_type must be one of {LOSSES.keys()}. Instead entered {loss_type}")
            
        criterion = LOSSES[loss_type]
            
        bs_tar, npts_tar, _ = input_points.shape
        npts_src = self.point_dataset.npts
        # assign random latent code close to zero
        embedded_latents = torch.nn.Parameter(torch.randn(bs_tar, self.lat_dims, device=self.device)*1e-4, 
                                              requires_grad=True)
        self.deformer.net.tar_latents = embedded_latents
        embedded_latents = self.deformer.net.tar_latents
        # [bs_tar, lat_dims]        
        
        # init optimizer
        if not optimizer in OPTIMIZERS.keys():
            raise ValueError(f"optimizer must be one of {OPTIMIZERS.keys()}")
        optim = OPTIMIZERS[optimizer]([embedded_latents], lr=lr)
        
        # scheduler
#         scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optim, mode='min', factor=0.3, patience=1, verbose=verbose, threshold=0.005, threshold_mode='rel')
        
        # init dataloader
        sampler = SubsetRandomSampler(np.arange(len(self.point_dataset)).tolist())
        point_loader = DataLoader(self.point_dataset, batch_size=bs, sampler=sampler,
                                  shuffle=False, drop_last=True)
        
        # chamfer distance calc
        chamfer_dist = ChamferDistKDTree(reduction='mean', njobs=1)
        chamfer_dist.to(self.device)

        def optimize_latent(point_loader, niter):
            # optimize for latents
            deformer.train()
            toc = time.time()
            
            bs_src = point_loader.batch_size
            embedded_latents_ = embedded_latents[None].expand(bs_src, bs_tar, self.lat_dims)
            # [bs_src, bs_tar, lat_dims]

            # broadcast and reshape input points
            target_points_ = (input_points[None]
                              .expand(bs_src, bs_tar, npts_tar, 3).view(-1, npts_tar, 3))
            
            for batch_idx, (fnames, idxs, source_points) in enumerate(point_loader):
                tic = time.time()
                # send tensors to device
                source_points = source_points.to(device)  # [bs_src, npts_src, 3]
                idxs = idxs.to(device)

                optim.zero_grad()

                # deform chosen points to input_points
                ## broadcast src lats to src x tar
                source_latents = self.lat_params[idxs]  # [bs_src, lat_dims]
                source_latents_ = source_latents[:, None].expand(bs_src, bs_tar, self.lat_dims)
                source_latents_ = source_latents_.view(-1, self.lat_dims)
                target_latents_ = embedded_latents_.view(-1, self.lat_dims)
                zeros = torch.zeros_like(source_latents_)
                source_target_latents = torch.stack([source_latents_, zeros, target_latents_], dim=1)

                ## broadcast src points to src x tar
                source_points_ = (source_points[:, None]
                                  .expand(bs_src, bs_tar, npts_src, 3).view(-1, npts_src, 3))

                deformed_pts = deformer(source_points,   # [bs_sr*bs_tar, npts_src, 3]
                                        source_target_latents)  # [bs_sr*bs_tar, npts_src, 3]

                # symmetric pair of matching losses
                if self.symm:
                    accu, comp, cham = chamfer_dist(utils.symmetric_duplication(deformed_pts, symm_dim=2), 
                                                    utils.symmetric_duplication(target_points_, 
                                                                                symm_dim=2))
                else:
                    accu, comp, cham = chamfer_dist(deformed_pts, 
                                                    target_points_)

                if matching == "one_way":
                    comp = torch.mean(comp, dim=1)
                    loss = criterion(comp, torch.zeros_like(comp))
                else:
                    loss = criterion(cham, torch.zeros_like(cham))

                # check amount of deformation
                deform_abs = torch.mean(torch.norm(deformed_pts - source_points, dim=-1))

                loss.backward()

                # gradient clipping
                torch.nn.utils.clip_grad_value_(embedded_latents, 1.)

                optim.step()
    #             scheduler.step(loss.item())

                toc = time.time()
                if verbose:
                    if loss_type == 'l1':
                        dist = loss.item()
                    else:
                        dist = np.sqrt(loss.item())
                    print(f"Loss: {loss.item():.4f}, Dist: {dist:.4f}, Deformation Magnitude: {deform_abs.item():.4f}, Time per iter (s): {toc-tic:.4f}")
                if batch_idx >= niter:
                    break
                    
        # optimize to range
        optimize_latent(point_loader, niter)
                
        # finetune topk
        dist, idxs = self.tree.query(embedded_latents.detach().cpu().numpy(), k=topk_finetune)  # [batch, k]
        bs, k = idxs.shape
        idxs_ = idxs.reshape(-1)
        
        # change lr
        for param_group in optim.param_groups:
            param_group['lr'] = 1e-3
        
        sampler = SubsetRandomSampler(idxs_.tolist()*10)
        point_loader = DataLoader(self.point_dataset, batch_size=topk_finetune, sampler=sampler,
                                  shuffle=False, drop_last=True)
        print("Finetuning for 10 iters...")
        optimize_latent(point_loader, 10)
        
        return embedded_latents.detach().cpu().numpy()
    
    def retrieve(self, lat_codes, tar_pts, topk=10, matching="one_way"):
        """Retrieve top 10 nearest neighbors, deform and pick the best one.
        
        Args: 
          lat_codes: tensor of shape [batch, lat_dims], latent code targets.
        
        Returns:
          List of len batch of (V, F) tuples.
        """
        if lat_codes.shape[0] != 1:
            raise NotImplementedError("Code is not ready for batch size > 1.")
        dist, idxs = self.tree.query(lat_codes, k=topk)  # [batch, k]
        bs, k = idxs.shape
        idxs_ = idxs.reshape(-1)
        
        if not isinstance(lat_codes, torch.Tensor):
            lat_codes = torch.tensor(lat_codes).float().to(self.device)
        
        src_latent = self.lat_params[idxs_]  # [batch*k, lat_dims]
        tar_latent = (lat_codes[:, None]
                      .expand(bs, k, self.lat_dims)
                      .reshape(-1, self.lat_dims))  # [batch*k, lat_dims]
        zeros = torch.zeros_like(src_latent)
        src_tar_latent = torch.stack([src_latent, zeros, tar_latent], dim=1)
        
        # retrieve meshes
        orig_meshes = [self.mesh_dataset.get_single(i) for i in idxs_]  # [(v1,f1), ..., (vn,fn)]
        src_verts, faces, nv = self._padded_verts_from_meshes(orig_meshes)
        src_verts = torch.tensor(src_verts).to(self.device)
        with torch.no_grad():
            deformed_verts = deformer(src_verts, src_tar_latent)
        deformed_meshes = self._meshes_from_padded_verts(deformed_verts, faces, nv)
        
        # chamfer distance calc
        chamfer_dist = ChamferDistKDTree(reduction='mean', njobs=1)
        chamfer_dist.to(self.device)
        dist = []
        for i in range(len(deformed_meshes)):
            accu, comp, cham = chamfer_dist(deformed_meshes[i][0][None].to(self.device), 
                                            torch.tensor(tar_pts)[None].to(self.device))
            if matching == "one_way": 
                dist.append(torch.mean(comp, dim=1).item())
            else:
                dist.append(cham.item())
        
        # reshape the list of (v, f) tuples
        deformed_meshes = [(vf[0].detach().cpu().numpy(), vf[1].detach().cpu().numpy()) for vf in deformed_meshes]
        
        return deformed_meshes, orig_meshes, dist
        
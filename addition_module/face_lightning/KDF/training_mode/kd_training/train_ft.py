"""
@author: Jun Wang
@date: 20201019
@contact: jun21wangustc@gmail.com
"""
import os
import sys
import shutil
import argparse
import logging as logger

import torch
from torch import optim
from torch.utils.data import DataLoader
from tensorboardX import SummaryWriter

from backbone.backbone_def import BackboneFactory
from loss.loss_def import KDLossFactory
from utils.net_util import define_paraphraser, define_translator

sys.path.append('../../../')
from utils.AverageMeter import AverageMeter
from data_processor.train_dataset import ImageDataset
from head.head_def import HeadFactory

logger.basicConfig(level=logger.INFO, 
                   format='%(levelname)s %(asctime)s %(filename)s: %(lineno)d] %(message)s',
                   datefmt='%Y-%m-%d %H:%M:%S')

class FaceModel(torch.nn.Module):
    """Define a traditional face model which contains a backbone and a head.
    
    Attributes:
        backbone(object): the backbone of face model.
        head(object): the head of face model.
    """
    def __init__(self, backbone_factory, backbone_type, head_factory):
        """Init face model by backbone factorcy and head factory.
        
        Args:
            backbone_factory(object): produce a backbone according to config files.
            head_factory(object): produce a head according to config files.
        """
        super(FaceModel, self).__init__()
        self.backbone = backbone_factory.get_backbone(backbone_type)
        self.head = head_factory.get_head()

    def forward(self, data, label):
        feat = self.backbone.forward(data)
        pred = self.head.forward(feat, label)
        return feat, pred

def get_lr(optimizer):
    """Get the current learning rate from optimizer. 
    """
    for param_group in optimizer.param_groups:
        return param_group['lr']

def train_para(train_loader, nets, optimizer_para, criterionPara, total_epoch):
	tnet        = nets['tnet']
	paraphraser = nets['paraphraser']
	paraphraser.train()
	for epoch in range(1, total_epoch+1):
		batch_time  = AverageMeter()
		data_time   = AverageMeter()
		para_losses = AverageMeter()
		epoch_start_time = time.time()
		end = time.time()
		for i, (img, _) in enumerate(train_loader, start=1):
			data_time.update(time.time() - end)
			if args.cuda:
				img = img.cuda()
			_, _, _, rb3_t, _, _ = tnet(img)
			_, rb3_t_rec = paraphraser(rb3_t[1].detach())
			para_loss = criterionPara(rb3_t_rec, rb3_t[1].detach())
			para_losses.update(para_loss.item(), img.size(0))
			optimizer_para.zero_grad()
			para_loss.backward()
			optimizer_para.step()
			batch_time.update(time.time() - end)
			end = time.time()
			if i % args.print_freq == 0:
				log_str = ('Epoch[{0}]:[{1:03}/{2:03}] '
						   'Time:{batch_time.val:.4f} '
						   'Data:{data_time.val:.4f}  '
						   'Para:{para_losses.val:.4f}({para_losses.avg:.4f})'.format(
						   epoch, i, len(train_loader), batch_time=batch_time, data_time=data_time,
						   para_losses=para_losses))
				logging.info(log_str)
		epoch_duration = time.time() - epoch_start_time
		logging.info('Epoch time: {}s'.format(int(epoch_duration)))

def train_one_epoch(data_loader, teacher_model, student_model, optimizer, 
                        criterion, criterion_kd, cur_epoch, loss_cls_meter, loss_kd_meter, conf):
    """Tain one epoch by traditional training.
    """
    for batch_idx, (images, labels) in enumerate(data_loader):
        images = images.to(conf.device)
        labels = labels.to(conf.device)
        labels = labels.squeeze()
        feats_s, preds_s = student_model.forward(images, labels)
        loss_cls = criterion(preds_s, labels)
        with torch.no_grad():
            feats_t, preds_t = teacher_model.forward(images, labels)
        if conf.loss_type == 'PKT':
            loss_kd = criterion_kd(feats_s, feats_t.detach()) * args.lambda_kd
        else:
            loss_kd = criterion_kd(preds_s, preds_t.detach()) * args.lambda_kd
        loss = loss_cls + loss_kd        
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        loss_cls_meter.update(loss_cls.item(), images.shape[0])
        loss_kd_meter.update(loss_kd.item(), images.shape[0])
        if batch_idx % conf.print_freq == 0:
            loss_cls_avg = loss_cls_meter.avg
            loss_kd_avg = loss_kd_meter.avg
            lr = get_lr(optimizer)
            logger.info('Epoch %d, iter %d/%d, lr %f, loss_cls %f, loss_kd %f.' % 
                        (cur_epoch, batch_idx, len(data_loader), lr, loss_cls_avg, loss_kd_avg))
            global_batch_idx = cur_epoch * len(data_loader) + batch_idx
            conf.writer.add_scalar('Cls_loss', loss_cls_avg, global_batch_idx)
            conf.writer.add_scalar('KD_loss', loss_kd_avg, global_batch_idx)
            conf.writer.add_scalar('Train_lr', lr, global_batch_idx)
            loss_cls_meter.reset()
            loss_kd_meter.reset()
        if (batch_idx + 1) % conf.save_freq == 0:
            saved_name = 'Epoch_%d_batch_%d.pt' % (cur_epoch, batch_idx)
            state = {
                'state_dict': student_model.module.state_dict(),
                'epoch': cur_epoch,
                'batch_id': batch_idx
            }
            torch.save(state, os.path.join(conf.out_dir, saved_name))
            logger.info('Save checkpoint %s to disk.' % saved_name)
    saved_name = 'Epoch_%d.pt' % cur_epoch
    state = {'state_dict': student_model.module.state_dict(), 
             'epoch': cur_epoch, 'batch_id': batch_idx}
    torch.save(state, os.path.join(conf.out_dir, saved_name))
    logger.info('Save checkpoint %s to disk...' % saved_name)

def train(conf):
    """Total training procedure.
    """
    data_loader = DataLoader(ImageDataset(conf.data_root, conf.train_file), 
                             conf.batch_size, True, num_workers = 4)

    paraphraser = define_paraphraser(in_channels_t, args.k, use_bn, args.cuda)
    translator = define_translator(in_channels_s, in_channels_t, args.k, use_bn, args.cuda)
    optimizer_para = torch.optim.SGD(paraphraser.parameters(),
         lr = args.lr * 0.1, 
         momentum = args.momentum, 
         weight_decay = args.weight_decay)
    criterionPara = torch.nn.MSELoss().cuda()
    logging.info('The first stage, training the paraphraser......')
    train_para(data_loader, nets, optimizer_para, criterionPara, 30)
    paraphraser.eval()
    for param in paraphraser.parameters():
        param.requires_grad = False
    logging.info('The second stage, training the student network......')










    conf.device = torch.device('cuda:0')
    criterion = torch.nn.CrossEntropyLoss().cuda(conf.device)
    backbone_factory = BackboneFactory(conf.backbone_conf_file)    
    head_factory = HeadFactory(conf.head_type, conf.head_conf_file)
    kd_loss_factory = KDLossFactory(conf.loss_type, conf.loss_conf_file)
    criterion_kd = kd_loss_factory.get_kd_loss().cuda(conf.device)

    backbone_factory = BackboneFactory(conf.backbone_conf_file)
    teacher_model = FaceModel(backbone_factory, args.teacher_backbone_type, head_factory)
    state_dict = torch.load(args.pretrained_teacher)['state_dict']
    teacher_model.load_state_dict(state_dict)
    teacher_model = torch.nn.DataParallel(teacher_model).cuda()
    student_model = FaceModel(backbone_factory, args.student_backbone_type, head_factory)
    ori_epoch = 0
    if conf.resume:
        ori_epoch = torch.load(args.pretrain_model)['epoch'] + 1
        state_dict = torch.load(args.pretrain_model)['state_dict']
        student_model.load_state_dict(state_dict)
    student_model = torch.nn.DataParallel(student_model).cuda()
    parameters = [p for p in student_model.parameters() if p.requires_grad]
    optimizer = optim.SGD(parameters, lr = conf.lr, 
                          momentum = conf.momentum, weight_decay = 1e-4)
    lr_schedule = optim.lr_scheduler.MultiStepLR(
        optimizer, milestones = conf.milestones, gamma = 0.1)
    loss_cls_meter = AverageMeter()
    loss_kd_meter = AverageMeter()
    student_model.train()
    for epoch in range(ori_epoch, conf.epoches):
        train_one_epoch(data_loader, teacher_model, student_model, optimizer, 
                        criterion, criterion_kd, epoch, loss_cls_meter, loss_kd_meter, conf)
        lr_schedule.step()                        

if __name__ == '__main__':
    conf = argparse.ArgumentParser(description='traditional_training for face recognition.')
    conf.add_argument("--data_root", type = str, 
                      help = "The root folder of training set.")
    conf.add_argument("--train_file", type = str,  
                      help = "The training file path.")
    conf.add_argument("--student_backbone_type", type = str, 
                      help = "Mobilefacenets, Resnet.")
    conf.add_argument("--teacher_backbone_type", type = str, 
                      help = "Mobilefacenets, Resnet.")
    conf.add_argument("--backbone_conf_file", type = str, 
                      help = "the path of backbone_conf.yaml.")
    conf.add_argument("--head_type", type = str, 
                      help = "mv-softmax, arcface, npc-face.")
    conf.add_argument("--head_conf_file", type = str, 
                      help = "the path of head_conf.yaml.")
    conf.add_argument("--loss_type", type = str, 
                      help = "Logits, PKT...")
    conf.add_argument("--loss_conf_file", type = str, 
                      help = "the path of loss_conf.yaml.")
    conf.add_argument('--lr', type = float, default = 0.1, 
                      help='The initial learning rate.')
    conf.add_argument('--lambda_kd', type = float, default = 1.0, 
                      help='The weight of kd loss.')
    conf.add_argument("--out_dir", type = str, 
                      help = "The folder to save models.")
    conf.add_argument('--epoches', type = int, default = 9, 
                      help = 'The training epoches.')
    conf.add_argument('--step', type = str, default = '2,5,7', 
                      help = 'Step for lr.')
    conf.add_argument('--print_freq', type = int, default = 10, 
                      help = 'The print frequency for training state.')
    conf.add_argument('--save_freq', type = int, default = 10, 
                      help = 'The save frequency for training state.')
    conf.add_argument('--batch_size', type = int, default = 128, 
                      help='The training batch size over all gpus.')
    conf.add_argument('--momentum', type = float, default = 0.9, 
                      help = 'The momentum for sgd.')
    conf.add_argument('--log_dir', type = str, default = 'log', 
                      help = 'The directory to save log.log')
    conf.add_argument('--tensorboardx_logdir', type = str, 
                      help = 'The directory to save tensorboardx logs')
    conf.add_argument('--pretrained_teacher', type = str, default = 'mv_epoch_8.pt', 
                      help = 'The path of pretrained teahcer model')
    conf.add_argument('--pretrained_model', type = str, default = 'mv_epoch_8.pt', 
                      help = 'The path of pretrained model')
    conf.add_argument('--resume', '-r', action = 'store_true', default = False, 
                      help = 'Whether to resume from a checkpoint.')
    args = conf.parse_args()
    args.milestones = [int(num) for num in args.step.split(',')]
    if not os.path.exists(args.out_dir):
        os.makedirs(args.out_dir)
    if not os.path.exists(args.log_dir):
        os.makedirs(args.log_dir)
    tensorboardx_logdir = os.path.join(args.log_dir, args.tensorboardx_logdir)
    if os.path.exists(tensorboardx_logdir):
        shutil.rmtree(tensorboardx_logdir)
    writer = SummaryWriter(log_dir=tensorboardx_logdir)
    args.writer = writer
    
    logger.info('Start optimization.')
    logger.info(args)
    train(args)
    logger.info('Optimization done!')

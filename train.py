import sys, os, argparse, time
import numpy as np
import cv2
import matplotlib.pyplot as plt
import shutil
import pandas as pd

import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torchvision import transforms
import torchvision
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.utils.model_zoo as model_zoo

from headpose.hopenet import Hopenet
from datasets import MyDatasets
from tensorboardX import SummaryWriter
from datasets import utils

def parse_args():
    """Parse input arguments."""
    parser = argparse.ArgumentParser(description='Head pose estimation using the Hopenet network.')
    parser.add_argument('--gpu', dest='gpu', help='GPU device id to use [0]',
            default=0, type=int)
    parser.add_argument('--epochs', dest='epochs', help='Maximum number of training epochs.',
          default=5, type=int)
    parser.add_argument('--batch_size', dest='batch_size', help='Batch size.',
          default=16, type=int)
    parser.add_argument('--lr', dest='lr', help='Base learning rate.',
          default=0.001, type=float)
    parser.add_argument('--dataset', dest='dataset', help='Dataset type.', default='Pose_300W_LP', type=str)
    parser.add_argument('--data_dir', dest='data_dir', help='Directory path for data.',
          default='', type=str)
    parser.add_argument('--filename_list', dest='filename_list', help='Path to text file containing relative paths for every example.',
          default='', type=str)
    parser.add_argument('--json_path', dest='json_path', help='Directory path for image bboxes info.',
          default='', type=str)
    parser.add_argument('--alpha', dest='alpha', help='Regression loss coefficient.',
          default=0.001, type=float)
    parser.add_argument('--snapshot', dest='snapshot', help='Path of model snapshot.',
          default='', type=str)
    parser.add_argument('--model', dest='model', help='model to use', default='resnet50', type=str)
    parser.add_argument('--test_data_dir', dest='test_data_dir', help='Path to test dataset', default='', type=str)
    parser.add_argument('--test_filename_list', dest='test_filename_list', help='Path to test file containing relative paths for test example.', default='', type=str)
    parser.add_argument('--test_json_path', dest='test_json_path', help='Directory path for image bboxes info.',
          default='', type=str)
    parser.add_argument('--output_dir', dest='output_dir', help='output dir.', default = '', type=str)
    args = parser.parse_args()
    return args


def get_ignored_params(model):
    # Generator function that yields ignored params.
    b = [model.conv1, model.bn1, model.fc_finetune]
    for i in range(len(b)):
        for module_name, module in b[i].named_modules():
            if 'bn' in module_name:
                module.eval()
            for name, param in module.named_parameters():
                yield param

def get_non_ignored_params(model):
    # Generator function that yields params that will be optimized.
    b = [model.layer1, model.layer2, model.layer3, model.layer4]
    for i in range(len(b)):
        for module_name, module in b[i].named_modules():
            if 'bn' in module_name:
                module.eval()
            for name, param in module.named_parameters():
                yield param

def get_fc_params(model):
    # Generator function that yields fc layer params.
    b = [model.fc_yaw, model.fc_pitch, model.fc_roll]
    for i in range(len(b)):
        for module_name, module in b[i].named_modules():
            for name, param in module.named_parameters():
                yield param

def load_filtered_state_dict(model, snapshot):
    # By user apaszke from discuss.pytorch.org
    model_dict = model.state_dict()
    snapshot = {k: v for k, v in snapshot.items() if k in model_dict}
    model_dict.update(snapshot)
    model.load_state_dict(model_dict)

def loss_mean(loss):
    loss_sum = 0
    for l in loss:
        loss_sum += l
    return loss_sum / len(loss)

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'output/best_snapshot/model_best.pth.tar')

if __name__ == '__main__':
    args = parse_args()

    cudnn.enabled = True
    num_epochs = args.epochs
    batch_size = args.batch_size
    gpu = args.gpu

    if not os.path.exists('output/snapshots'):
        os.makedirs('output/snapshots')
    if not os.path.exists('output/best_snapshot'):
        os.makedirs('output/best_snapshot')


    # ResNet50 structure
    if args.model == 'resnet50':
        model = Hopenet(torchvision.models.resnet.Bottleneck, [3, 4, 6, 3], 66)
    elif args.model == 'resnet18':
        model = Hopenet(torchvision.models.resnet.BasicBlock, [2, 2, 2, 2], 66)
    elif args.model == 'resnet152':
        model = Hopenet(torchvision.models.resnet.Bottleneck, [3, 8, 36, 3], 66)


    if args.snapshot == '':
        print("===> Trained without resume, load imagenet pretrained model: '{}'".format(args.model))
        if args.model == 'resnet50':
            load_filtered_state_dict(model, model_zoo.load_url('https://download.pytorch.org/models/resnet50-19c8e357.pth'))
        elif args.model == 'resnet18':
            load_filtered_state_dict(model, model_zoo.load_url('https://download.pytorch.org/models/resnet18-5c106cde.pth'))
        elif args.model == 'resnet152':
            load_filtered_state_dict(model, model_zoo.load_url('https://download.pytorch.org/models/resnet152-b121ed2d.pth'))
    else:
        saved_state_dict = torch.load(args.snapshot)
        model.load_state_dict(saved_state_dict['state_dict'])
        print("===> loaded snashot '{}' (epoch {})".format(args.snapshot,saved_state_dict['epoch']))

    print ('===> Loading data.')
    transformations = transforms.Compose([
                    transforms.Resize((112,112)),
                    #transforms.RandomCrop(224),
                    transforms.ToTensor(),
                    transforms.Normalize(mean=[0.485, 0.456, 0.406], 
                                            std=[0.229, 0.224, 0.225])])

    if args.dataset == 'Pose_300W_LP':
        pose_dataset = MyDatasets.Pose_300W_LP(args.data_dir, args.filename_list, transformations)
    elif args.dataset == 'Pose_300W_LP_random_ds':
        pose_dataset = MyDatasets.Pose_300W_LP_random_ds(args.data_dir, args.filename_list, transformations)
    elif args.dataset == 'Synhead':
        pose_dataset = MyDatasets.Synhead(args.data_dir, args.filename_list, transformations)
    elif args.dataset == 'AFLW2000':
        pose_dataset = MyDatasets.AFLW2000(args.data_dir, args.filename_list, transformations)
    elif args.dataset == 'BIWI':
        pose_dataset = MyDatasets.BIWI(args.data_dir, args.filename_list, transformations)
    elif args.dataset == 'AFLW':
        pose_dataset = MyDatasets.AFLW(args.data_dir, args.filename_list, transformations)
    elif args.dataset == 'AFLW_aug':
        pose_dataset = MyDatasets.AFLW_aug(args.data_dir, args.filename_list, transformations)
    elif args.dataset == 'AFW':
        pose_dataset = MyDatasets.AFW(args.data_dir, args.filename_list, transformations)
    elif args.dataset =='XMCTest':
        pose_dataset = MyDatasets.XMCTestData(args.data_dir, args.data_dir, args.json_path, args.filename_list, transformations)
    else:
        print ('Error: not a valid dataset name')
        sys.exit()

    train_loader = torch.utils.data.DataLoader(dataset=pose_dataset,
                                               batch_size=batch_size,
                                               shuffle=True,
                                               num_workers=8,
                                               pin_memory=True)

    # test_pose_dataset = MyDatasets.AFLW2000(args.test_data_dir, 
    #                                         args.test_filename_list, 
    #                                         transformations)

    test_pose_dataset = MyDatasets.XMCTestData(args.test_data_dir, 
                                                args.test_data_dir,
                                                args.test_json_path,
                                                args.test_filename_list, 
                                                transformations)

    test_loader = torch.utils.data.DataLoader(dataset=test_pose_dataset,
                                                batch_size=args.batch_size, 
                                                shuffle=False,
                                                num_workers=8,
                                                pin_memory=True)


    model.cuda(gpu)
    criterion = nn.CrossEntropyLoss().cuda(gpu)
    reg_criterion = nn.MSELoss().cuda(gpu)
    # Regression loss coefficient
    alpha = args.alpha

    softmax = nn.Softmax(dim=1).cuda(gpu)
    idx_tensor = [idx for idx in range(66)]
    idx_tensor = Variable(torch.FloatTensor(idx_tensor)).cuda(gpu)

    optimizer = torch.optim.Adam([{'params': get_ignored_params(model), 'lr': 0},
                                  {'params': get_non_ignored_params(model), 'lr': args.lr},
                                  {'params': get_fc_params(model), 'lr': args.lr * 5}],
                                   lr = args.lr)


    writer = SummaryWriter('logs')
    print ('===> Ready to train network.')

    train_loss = []
    test_loss = []
    for epoch in range(num_epochs):
        epoch_loss_yaw = []
        epoch_loss_pitch = []
        epoch_loss_roll = []
        epoch_loss_mae = []
        for i, (images, labels, cont_labels, name) in enumerate(train_loader):
            images = Variable(images).cuda(gpu)

            # Binned labels
            label_yaw = Variable(labels[:,0]).cuda(gpu)
            label_pitch = Variable(labels[:,1]).cuda(gpu)
            label_roll = Variable(labels[:,2]).cuda(gpu)

            # Continuous labels
            label_yaw_cont = Variable(cont_labels[:,0]).cuda(gpu)
            label_pitch_cont = Variable(cont_labels[:,1]).cuda(gpu)
            label_roll_cont = Variable(cont_labels[:,2]).cuda(gpu)

            # Forward pass
            yaw, pitch, roll = model(images)

            # Cross entropy loss
            loss_yaw = criterion(yaw, label_yaw)
            loss_pitch = criterion(pitch, label_pitch)
            loss_roll = criterion(roll, label_roll)

            # MSE loss
            yaw_predicted = softmax(yaw)
            pitch_predicted = softmax(pitch)
            roll_predicted = softmax(roll)

            yaw_predicted = torch.sum(yaw_predicted * idx_tensor, 1) * 3 - 99
            pitch_predicted = torch.sum(pitch_predicted * idx_tensor, 1) * 3 - 99
            roll_predicted = torch.sum(roll_predicted * idx_tensor, 1) * 3 - 99

            loss_reg_yaw = reg_criterion(yaw_predicted, label_yaw_cont)
            loss_reg_pitch = reg_criterion(pitch_predicted, label_pitch_cont)
            loss_reg_roll = reg_criterion(roll_predicted, label_roll_cont)

            # Total loss
            loss_yaw += alpha * loss_reg_yaw
            loss_pitch += alpha * loss_reg_pitch
            loss_roll += alpha * loss_reg_roll

            loss_seq = [loss_yaw, loss_pitch, loss_roll]
            grad_seq = [torch.ones(1).cuda(gpu) for _ in range(len(loss_seq))]
            optimizer.zero_grad()
            torch.autograd.backward(loss_seq, grad_seq)
            optimizer.step()

            mae = (loss_yaw + loss_pitch + loss_roll)/3

            epoch_loss_yaw.append(loss_yaw)
            epoch_loss_pitch.append(loss_pitch)
            epoch_loss_roll.append(loss_roll)
            epoch_loss_mae.append(mae)

            if (i+1) % 10 == 0:
                print ('Epoch [%d/%d], Iter [%d/%d] Losses: Yaw %.4f, Pitch %.4f, Roll %.4f, MAE %.5f'
                       %(epoch+1, num_epochs, i+1, len(pose_dataset)//batch_size, loss_yaw.item(), loss_pitch.item(), loss_roll.item(), mae.item()))

        train_loss.append([epoch, loss_mean(epoch_loss_yaw).item(), loss_mean(epoch_loss_pitch).item(),loss_mean(epoch_loss_roll).item(),loss_mean(epoch_loss_mae).item()])

        writer.add_scalar('epoch_loss/yaw', loss_mean(epoch_loss_yaw), epoch)
        writer.add_scalar('epoch_loss/pitch', loss_mean(epoch_loss_pitch), epoch)
        writer.add_scalar('epoch_loss/roll', loss_mean(epoch_loss_roll), epoch)
        writer.add_scalar('epoch_loss/mae', loss_mean(epoch_loss_mae), epoch)


        # test model 
        ##-------------------------------------------------------------------

        model.eval()  # Change model to 'eval' mode (BN uses moving mean/var).
        total = 0

        idx_tensor = [idx for idx in range(66)]
        idx_tensor = torch.FloatTensor(idx_tensor).cuda(gpu)

        yaw_error = .0
        pitch_error = .0
        roll_error = .0

        for i, (images, labels, cont_labels, name) in enumerate(test_loader):
            images = Variable(images).cuda(gpu)
            total += cont_labels.size(0)

            label_yaw = cont_labels[:,0].float()
            label_pitch = cont_labels[:,1].float()
            label_roll = -cont_labels[:,2].float()

            yaw, pitch, roll = model(images)

            # Binned predictions
            _, yaw_bpred = torch.max(yaw.data, 1)
            _, pitch_bpred = torch.max(pitch.data, 1)
            _, roll_bpred = torch.max(roll.data, 1)

            # Continuous predictions
            yaw_predicted = utils.softmax_temperature(yaw.data, 1)
            pitch_predicted = utils.softmax_temperature(pitch.data, 1)
            roll_predicted = utils.softmax_temperature(roll.data, 1)

            yaw_predicted = torch.sum(yaw_predicted * idx_tensor, 1).cpu() * 3 - 99
            pitch_predicted = torch.sum(pitch_predicted * idx_tensor, 1).cpu() * 3 - 99
            roll_predicted = torch.sum(roll_predicted * idx_tensor, 1).cpu() * 3 - 99

            # Mean absolute error
            yaw_error += torch.sum(torch.abs(yaw_predicted - label_yaw))
            pitch_error += torch.sum(torch.abs(pitch_predicted - label_pitch))
            roll_error += torch.sum(torch.abs(roll_predicted - label_roll))

            mae = (yaw_error + pitch_error + roll_error) / 3

            if (i+1) % 10 == 0:
                print ('Test Epoch [%d/%d], Iter [%d/%d] Losses: Yaw %.4f, Pitch %.4f, Roll %.4f, MAE %.4f'
                       %(epoch+1, num_epochs, i+1, len(test_pose_dataset)//batch_size, yaw_error/total, pitch_error/total, roll_error/total, mae/total))

        writer.add_scalar('test_error/yaw',   (yaw_error/total), epoch)
        writer.add_scalar('test_error/pitch', (pitch_error/total), epoch)
        writer.add_scalar('test_error/roll', (roll_error/total), epoch)
        writer.add_scalar('test_error/mae', (mae/total), epoch)

        test_mae = (yaw_error + pitch_error + roll_error) / (total * 3)
        
        test_loss.append([epoch,  yaw_error.item()/total, pitch_error.item()/total, roll_error.item()/total, test_mae.item()])

        print('Test error in degrees of the model on the ' + str(total) +
            ' test images. Yaw: %.4f, Pitch: %.4f, Roll: %.4f, MAE: %.4f' % (yaw_error / total,
                    pitch_error / total, roll_error / total, test_mae))


        if epoch == 0:
            minmum_loss = test_mae
            is_best = True
        else:
            is_best = test_mae < minmum_loss
        # Save models at numbered epochs.
        if epoch % 1 == 0 and epoch < num_epochs:
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.model,
                'state_dict': model.state_dict(),
                'optimizer' : optimizer.state_dict(),
            }, is_best, filename = os.path.join(args.output_dir, args.model + '_' + str(epoch) + '.pth'))

    name=['epoch','yaw','pitch','roll','mae']
    train=pd.DataFrame(columns=name,data=train_loss)
    train.to_csv(os.path.join(args.output_dir, args.model + '_' + str(args.alpha) + '_train_result.csv'),index=None)
    test=pd.DataFrame(columns=name,data=test_loss)
    test.to_csv(os.path.join(args.output_dir, args.model +'_' + str(args.alpha) + '_test_result.csv'), index=None)
    writer.close()
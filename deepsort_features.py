import sys
sys.path.insert(0, './yolov5')

import argparse
import os
import platform
import shutil
import time
from pathlib import Path

import cv2
import torch
import torch.backends.cudnn as cudnn
from numpy import random
from numpy import where
# libs to save feature arrays
import csv
import pickle
from numpy import savetxt, save, savez_compressed 

from models.experimental import attempt_load
from utils.datasets import LoadStreams, LoadImages
from utils.general import (
    check_img_size, non_max_suppression, apply_classifier, scale_coords,
    xyxy2xywh, plot_one_box, strip_optimizer, set_logging)
from utils.torch_utils import select_device, load_classifier, time_synchronized

# deep sort part
from libraries.deep_sort.utils.parser import get_config
from libraries.deep_sort.deep_sort import DeepSort

def detect(save_img=False):
    out, source, weights, view_img, save_txt, imgsz = \
        opt.output, opt.source, opt.weights, opt.view_img, opt.save_txt, opt.img_size
    webcam = source.isnumeric() or source.startswith('rtsp') or source.startswith('http') or source.endswith('.txt')

    # Initialize
    set_logging()
    device = select_device(opt.device)
    if os.path.exists(out):
        shutil.rmtree(out)           # delete output folder
    if os.path.exists(opt.features):
        shutil.rmtree(opt.features)  # delete features output folder
    if os.path.exists(opt.crops):
        shutil.rmtree(opt.crops)  # delete output folder with object crops
    os.makedirs(out)           # make new output folder
    os.makedirs(opt.features)  # make new output folder
    os.makedirs(opt.crops)     # make new output folder
    half = device.type != 'cpu'  # half precision only supported on CUDA

    # Load model
    model = attempt_load(weights, map_location=device)  # load FP32 model
    imgsz = check_img_size(imgsz, s=model.stride.max())  # check img_size
    if half:
        model.half()  # to FP16

    # Second-stage classifier
    classify = False
    if classify:
        modelc = load_classifier(name='resnet101', n=2)  # initialize
        modelc.load_state_dict(torch.load('weights/resnet101.pt', map_location=device)['model'])  # load weights
        modelc.to(device).eval()

    # Set Dataloader
    vid_path, vid_writer = None, None
    if webcam:
        view_img = True
        cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(source, img_size=imgsz)
    else:
        save_img = True
        dataset = LoadImages(source, img_size=imgsz)

    # log file dictionary: save frames when track_id object is detected
    log_frames = {"FPS": dataset.cap.get(cv2.CAP_PROP_FPS)}
    print("FRAMES PER SECOND ", dataset.cap.get(cv2.CAP_PROP_FPS))

    # Get names and colors
    names = model.module.names if hasattr(model, 'module') else model.names
    colors = [[random.randint(0, 255) for _ in range(3)] for _ in range(len(names))]

    # Find index corresponding to a person
    idx_person = names.index("person")

    # Deep SORT: initialize the tracker
    cfg = get_config()
    cfg.merge_from_file(opt.config_deepsort)
    deepsort = DeepSort(cfg.DEEPSORT.REID_CKPT,
                        max_dist=cfg.DEEPSORT.MAX_DIST, min_confidence=cfg.DEEPSORT.MIN_CONFIDENCE,
                        nms_max_overlap=cfg.DEEPSORT.NMS_MAX_OVERLAP, max_iou_distance=cfg.DEEPSORT.MAX_IOU_DISTANCE,
                        max_age=cfg.DEEPSORT.MAX_AGE, n_init=cfg.DEEPSORT.N_INIT, nn_budget=cfg.DEEPSORT.NN_BUDGET,
                        use_cuda=True)

    # Run inference
    t0 = time.time()
    img = torch.zeros((1, 3, imgsz, imgsz), device=device)  # init img
    _ = model(img.half() if half else img) if device.type != 'cpu' else None  # run once
    for path, img, im0s, vid_cap in dataset:
        img = torch.from_numpy(img).to(device)
        img = img.half() if half else img.float()  # uint8 to fp16/32
        img /= 255.0  # 0 - 255 to 0.0 - 1.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)

        # Inference
        t1 = time_synchronized()
        pred = model(img, augment=opt.augment)[0]

        # Apply NMS
        pred = non_max_suppression(pred, opt.conf_thres, opt.iou_thres, classes=opt.classes, agnostic=opt.agnostic_nms)
        t2 = time_synchronized()

        # Apply Classifier
        if classify:
            pred = apply_classifier(pred, modelc, img, im0s)
        # Process detections
        for i, det in enumerate(pred):  # detections per image
            if webcam:  # batch_size >= 1
                p, s, im0 = path[i], '%g: ' % i, im0s[i].copy()
            else:
                p, s, im0 = path, '', im0s

            save_path = str(Path(out) / Path(p).name)
            txt_path = str(Path(out) / Path(p).stem) + ('_%g' % dataset.frame if dataset.mode == 'video' else '')
            s += '%gx%g ' % img.shape[2:]  # print string
            gn = torch.tensor(im0.shape)[[1, 0, 1, 0]]  # normalization gain whwh
            if det is not None and len(det):
                # Rescale boxes from img_size to im0 size
                det[:, :4] = scale_coords(img.shape[2:], det[:, :4], im0.shape).round()

                # Print results
                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()  # detections per class
                    s += '%g %ss, ' % (n, names[int(c)])  # add to string

                # Deep SORT: person class only
                idxs_ppl = (det[:,-1] == idx_person).nonzero(as_tuple=False).squeeze(dim=1)   # 1. List of indices with 'person' class detections
                dets_ppl = det[idxs_ppl,:-1]                                                  # 2. Torch.tensor with 'person' detections
                print('\n {} people were detected!'.format(len(idxs_ppl)))

                # Deep SORT: convert data into a proper format
                xywhs = xyxy2xywh(dets_ppl[:,:-1]).to("cpu")
                confs = dets_ppl[:,4].to("cpu")

                # Deep SORT: feed detections to the tracker 
                if len(dets_ppl) != 0:
                    trackers, features = deepsort.update(xywhs, confs, im0)
                    for d in trackers:
                        ##### DEEP SORT feature object saver ####
                        track_id = d[4]
                        fname_features = opt.features+'/ID_{}'.format(track_id)
                        fname_crops = opt.crops+'/ID_{}'.format(track_id) 
                        if not os.path.exists(fname_features):
                            os.mkdir(fname_features)
                            os.mkdir(fname_crops)
                            log_frames['ID_'+str(track_id)] = []

                        # choose format to save feature arrays on your machine: 
                        # https://machinelearningmastery.com/how-to-save-a-numpy-array-to-file-for-machine-learning/
                        save_format = 'csv'
                        filename = fname_features+"/feature_frame_"+str(dataset.frame)
                        if save_format == 'csv':
                            savetxt(filename+'.csv', features[track_id], delimiter=',')
                            #data = numpy.loadtxt('data.csv', delimiter=',')
                        elif save_format == 'npy':
                            save(filename+'.npy', features[track_id])
                            #data = numpy.load('data.npy')
                        elif save_format == 'npz':
                            savez_compressed(filename+'.npz', features[track_id])
                            # dict_data = load('data.npz'); data = dict_data['arr_0']
                        # update log file with track_id detection history
                        log_frames['ID_'+str(track_id)].append(dataset.frame)
                        # save croped image
                        im_crop = im0[d[1]:d[3], d[0]:d[2], :]
                        cv2.imwrite(filename=fname_crops+"/image_crop_"+str(dataset.frame)+'.jpg', img=im_crop)
                        plot_one_box(d[:4], im0, label='ID'+str(int(d[4])), color=colors[1], line_thickness=1)

            # DEEP SORT: save updated log file
            log_format = 'txt'
            if log_format == 'txt':
                f_log = open(opt.features+"/log_detection.txt","w")
                f_log.write( str(log_frames) )
            elif log_format == 'pkl':
                f_log = open(opt.features+"/log_detection.pkl","wb")
                pickle.dump(log_frames,f_log)
            f_log.close()
            ###################################

            # Print time (inference + NMS)
            print('%sDone. (%.3fs)' % (s, t2 - t1))

            # Stream results
            if view_img:
                cv2.imshow(p, im0)
                if cv2.waitKey(1) == ord('q'):  # q to quit
                    raise StopIteration

            # Save results (image with detections)
            if save_img:
                if dataset.mode == 'images':
                    cv2.imwrite(save_path, im0)
                else:
                    if vid_path != save_path:  # new video
                        vid_path = save_path
                        if isinstance(vid_writer, cv2.VideoWriter):
                            vid_writer.release()  # release previous video writer

                        fourcc = 'mp4v'  # output video codec
                        fps = vid_cap.get(cv2.CAP_PROP_FPS)
                        w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                        h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        vid_writer = cv2.VideoWriter(save_path, cv2.VideoWriter_fourcc(*fourcc), fps, (w, h))
                    vid_writer.write(im0)
    if save_txt or save_img:
        print('Results saved to %s' % Path(out))
        if platform.system() == 'Darwin' and not opt.update:  # MacOS
            os.system('open ' + save_path)

    print('Done. (%.3fs)' % (time.time() - t0))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--weights', nargs='+', type=str, default='yolov5s.pt', help='model.pt path(s)')
    parser.add_argument('--source', type=str, default='inference/images', help='source')                      # file/folder, 0 for webcam
    parser.add_argument('--output', type=str, default='inference/output', help='output folder')               # output folder
    parser.add_argument('--features', type=str, default='inference/features', help='features output folder')  # feature output folder
    parser.add_argument('--crops', type=str, default='inference/image_crops', help='save object crops')       # save object crops
    parser.add_argument('--img-size', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.4, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.5, help='IOU threshold for NMS')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--view-img', action='store_true', help='display results')
    parser.add_argument('--save-txt', action='store_true', help='save results to *.txt')
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --class 0, or --class 0 2 3')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--update', action='store_true', help='update all models')
    parser.add_argument("--config_deepsort", type=str, default="deep_sort/configs/deep_sort.yaml")
    opt = parser.parse_args()
    print(opt)

    with torch.no_grad():
        if opt.update:  # update all models (to fix SourceChangeWarning)
            for opt.weights in ['yolov5s.pt', 'yolov5m.pt', 'yolov5l.pt', 'yolov5x.pt']:
                detect()
                strip_optimizer(opt.weights)
        else:
            detect()

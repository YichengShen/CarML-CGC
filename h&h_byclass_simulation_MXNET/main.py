from collections import defaultdict
import copy
from sumo import SUMO_Dataset
from central_server import Central_Server, Simulation
from vehicle import Vehicle

import yaml
from locationPicker_v3 import output_junctions
import xml.etree.ElementTree as ET 

import mxnet as mx
from mxnet import nd, autograd, gluon
from mxnet.gluon.data.vision import transforms
from gluoncv.data import transforms as gcv_transforms
import numpy as np
import time, random, argparse, itertools


def parse_args():
    parser = argparse.ArgumentParser(description='Train a model for image classification.')
    parser.add_argument('--num-gpus', type=int, default=0,
                        help='number of gpus to use.')
    parser.add_argument('--num-round', type=int, default=0,
                        help='number of round.')
    opt = parser.parse_args()
    return opt

import sys
print(' '.join(sys.argv))

file = open('config.yml', 'r')
cfg = yaml.load(file, Loader=yaml.FullLoader)

def simulate(simulation):
    tree = ET.parse(simulation.FCD_file)
    root = tree.getroot()
    simulation.new_epoch()
    
    # Maximum training epochs
    while simulation.num_epoch <= cfg['neural_network']['epoch']:
        # For each time step (sec) in the FCD file 
        for timestep in root:

            if simulation.num_epoch > cfg['neural_network']['epoch']:
                break

            # For each vehicle on the map at the timestep
            for vehicle in timestep.findall('vehicle'):
                # If vehicle not yet stored in vehicle_dict
                if vehicle.attrib['id'] not in simulation.vehicle_dict:
                    simulation.add_into_vehicle_dict(vehicle)
                # Get the vehicle object from vehicle_dict
                vehi = simulation.vehicle_dict[vehicle.attrib['id']]  
                # Set location and speed
                vehi.set_properties(float(vehicle.attrib['x']),
                                    float(vehicle.attrib['y']),
                                    float(vehicle.attrib['speed']))

                closest_rsu = vehi.closest_rsu(simulation.rsu_list)
                if closest_rsu is not None:
                    # Download Training Data / New Epoch
                    if cfg['data_distribution'] == 'random':
                        if simulation.training_data:
                            vehi.training_data_assigned, vehi.training_label_assigned = simulation.training_data.pop()
                        else:
                            if simulation.num_epoch <= 10 or simulation.num_epoch % 10 == 0:
                                simulation.print_accuracy()
                            simulation.new_epoch()
                            vehi.training_data_assigned, vehi.training_label_assigned = simulation.training_data.pop()
                    elif cfg['data_distribution'] == 'byclass':
                        rsu_id = int(closest_rsu.rsu_id[-1])
                        if any(len(x) >= 100 for x in simulation.training_data_byclass):
                            if len(simulation.training_data_byclass[rsu_id]) >= 100:  
                                vehi.training_data_assigned = nd.array(np.array(simulation.training_data_byclass[rsu_id][:100]))
                                del simulation.training_data_byclass[rsu_id][:100]
                                vehi.training_label_assigned = nd.array(np.array(simulation.training_label_byclass[rsu_id][:100]))
                                del simulation.training_label_byclass[rsu_id][:100]
                                
                            # else:
                            #     continue
                                vehi.download_model_from(simulation.central_server)
                                vehi.compute_and_upload(simulation, closest_rsu)
                        else:
                            if simulation.num_epoch <= 10 or simulation.num_epoch % 10 == 0:
                                simulation.print_accuracy()
                            simulation.new_epoch()
                            if len(simulation.training_data_byclass[rsu_id]) >= 100:
                                
                                vehi.training_data_assigned = nd.array(np.array(simulation.training_data_byclass[rsu_id][:100]))
                                del simulation.training_data_byclass[rsu_id][:100]
                                vehi.training_label_assigned = nd.array(np.array(simulation.training_label_byclass[rsu_id][:100]))
                                del simulation.training_label_byclass[rsu_id][:100]
                            # else:
                            #     continue
                                vehi.download_model_from(simulation.central_server)
                                vehi.compute_and_upload(simulation, closest_rsu)

                    # vehi.download_model_from(simulation.central_server)
                    # print('download')
                    # vehi.compute_and_upload(simulation, closest_rsu)
                    # print('compute')
                
    return simulation.central_server.net


def main():
    opt = parse_args()

    num_gpus = opt.num_gpus
    context = [mx.gpu(i) for i in range(num_gpus)] if num_gpus > 0 else [mx.cpu()]

    num_round = opt.num_round

    ROU_FILE = cfg['simulation']['ROU_FILE']
    NET_FILE = cfg['simulation']['NET_FILE']
    FCD_FILE = cfg['simulation']['FCD_FILE']
    
    RSU_RANGE = cfg['comm_range']['v2rsu']           # range of RSU
    NUM_RSU = cfg['simulation']['num_rsu']           # number of RSU

    sumo_data = SUMO_Dataset(ROU_FILE, NET_FILE)
    vehicle_dict = {}
    rsu_list = sumo_data.rsuList(RSU_RANGE, NUM_RSU, output_junctions)
    # rsu_list = sumo_data.rsuList_random(RSU_RANGE, NUM_RSU)
    central_server = Central_Server(context, rsu_list)


    def transform(data, label):
        if cfg['dataset'] == 'cifar10':
            data = mx.nd.transpose(data, (2,0,1))
        data = data.astype(np.float32) / 255
        return data, label

    # Load Data
    batch_size = cfg['neural_network']['batch_size']
    num_training_data = cfg['num_training_data']
    train_data_byclass = None
    train_data_byrsu = None
    if cfg['dataset'] == 'cifar10':
        if cfg['data_distribution'] == 'random':
            train_data = mx.gluon.data.DataLoader(mx.gluon.data.vision.CIFAR10('../data/cx2', train=True, transform=transform).take(num_training_data),
                                batch_size, shuffle=True, last_batch='discard')
        elif cfg['data_distribution'] == 'byclass':
            train_data = mx.gluon.data.DataLoader(mx.gluon.data.vision.CIFAR10('../data/cx2', train=True, transform=transform).take(num_training_data),
                                batch_size=num_training_data, shuffle=True, last_batch='discard')
            
            for i in train_data:
                X, y = i
                X = list(X.asnumpy())
                y = list(y.asnumpy())
                X_first_half = X[:int(len(X)/2)]
                y_first_half = y[:int(len(y)/2)]
                X_second_half = X[int(len(X)/2):]
                y_second_half = y[int(len(y)/2):]
            
            train_data_byclass = defaultdict(list)

            for j in range(len(X_second_half)):
                train_data_byclass[y_second_half[j]].append(X_second_half[j])


        val_train_data = mx.gluon.data.DataLoader(mx.gluon.data.vision.CIFAR10('../data/cx2', train=True, transform=transform).take(num_training_data),
                                    batch_size, shuffle=False, last_batch='keep')
        val_test_data = mx.gluon.data.DataLoader(mx.gluon.data.vision.CIFAR10('../data/cx2', train=False, transform=transform),
                                    batch_size, shuffle=False, last_batch='keep')
    elif cfg['dataset'] == 'mnist':
        if cfg['data_distribution'] == 'random':
            train_data = mx.gluon.data.DataLoader(mx.gluon.data.vision.MNIST('../data/mnist', train=True, transform=transform).take(num_training_data),
                                batch_size, shuffle=True, last_batch='discard')            
        elif cfg['data_distribution'] == 'byclass':
            train_data = mx.gluon.data.DataLoader(mx.gluon.data.vision.MNIST('../data/mnist', train=True, transform=transform).take(num_training_data),
                                        batch_size=num_training_data, shuffle=True, last_batch='keep')
            train_data_byclass = defaultdict(list)
            for i in train_data:
                for j in range(num_training_data):
                    train_data_byclass[i[1][j].asnumpy()[0]].append(i[0][j].asnumpy())

            labels_ = list(train_data_byclass.keys())
            data_ = list(train_data_byclass.values())
            data_full = copy.deepcopy(data_)
            prop1 = 0.4
            prop2 = 0.3
            prop3 = 0.3
            train_data_byrsu = {}
            for i in range(10):
                if i == 8:
                    j = 9
                    k = 0
                elif i == 9:
                    j = 0
                    k = 1
                else:
                    j = i+1
                    k = i+2
                X = data_[i][:int(prop1*len(data_full[i]))] + data_[j][:int(prop2*len(data_full[j]))] + data_[k][:int(prop3*len(data_full[k]))]
                # print(f'{i}:{len(data_[i])}, {j}:{len(data_[j])}, {k}:{len(data_[k])}')
                del data_[i][:int(prop1*len(data_full[i]))]
                del data_[j][:int(prop2*len(data_full[j]))]
                del data_[k][:int(prop3*len(data_full[k]))]
                # print(f'{i}:{len(data_[i])}, {j}:{len(data_[j])}, {k}:{len(data_[k])}')
                y = [labels_[i] for _ in range(int(prop1*len(data_full[i])))] + [labels_[j] for _ in range(int(prop2*len(data_full[j])))] + [labels_[k] for _ in range(int(prop3*len(data_full[k])))]
                
                train_data_byrsu[i] = (X, y)
            

            for i in range(10):
                train_data_byrsu[i][0].extend(data_[i])
                train_data_byrsu[i][1].extend([labels_[i] for _ in range(len(data_[i]))])
                # print(len(train_data_byrsu[i][0]))
                # print(len(train_data_byrsu[i][1]))

            
        val_train_data = mx.gluon.data.DataLoader(mx.gluon.data.vision.MNIST('../data/mnist', train=True, transform=transform).take(num_training_data),
                                    batch_size, shuffle=False, last_batch='keep')
        val_test_data = mx.gluon.data.DataLoader(mx.gluon.data.vision.MNIST('../data/mnist', train=False, transform=transform),
                                    batch_size, shuffle=False, last_batch='keep')

    simulation = Simulation(FCD_FILE, vehicle_dict, rsu_list, central_server, train_data, val_train_data, val_test_data, train_data_byclass, X_first_half, y_first_half, num_round)
    model = simulate(simulation)

    # # Test the accuracy of the computed model
    # test_accuracy = tf.keras.metrics.Accuracy()     
    # for (x, y) in test_dataset:
    #     logits = model(x, training=False) 
    #     prediction = tf.argmax(logits, axis=1, output_type=tf.int32)
    #     test_accuracy(prediction, y)
    # print("Test set accuracy: {:.3%}".format(test_accuracy.result()))


if __name__ == '__main__':
    main()

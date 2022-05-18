import os
import yaml
import logging
import argparse
import numpy as np

import torch
import torch.optim as optim
from torch.cuda.amp import GradScaler, autocast
from torch.utils.tensorboard import SummaryWriter
from transformers import GPT2Tokenizer

from dataset import get_data
from model import Net


def generate_text(model, tokenizer, speaker_name_prompts):

    for i in range(len(speaker_name_prompts)):
        inputs = tokenizer(speaker_name_prompts[i], return_tensors="pt").to(device)

        gen_tokens = model.gpt_neo.generate(
            inputs.input_ids,
            do_sample=True,
            max_length=cfg['max_seq_len'],
            top_k=50,
            top_p=0.92,
            num_return_sequences=2
        )

        for temp_gen_token in gen_tokens:
            gen_text = tokenizer.decode(temp_gen_token,skip_special_tokens=True)
            generated_text_logger.info(gen_text)

    return

def train(cfg, device):

    train_dataloader, tokenizer = get_data(cfg, split=0)
    performance_logger.info('train data loaded.')

    valid_dataloader, _ = get_data(cfg, split=1)
    performance_logger.info('valid data loaded.\n')

    net = Net().to(device)

    optimizer = optim.Adam(net.parameters(), lr=cfg['learning_rate'])
    scaler = GradScaler()
    # train and validation writers will be different
    #train_writer = SummaryWriter(log_dir=os.path.join(cfg['log_dir'],cfg['experiment_name'],'train'))
    
    lowest_train_loss = float('inf') # should be val loss for checkpointing

    # zero the parameters' gradients
    optimizer.zero_grad()

    # TEMPORARY: generate speech before fine-tuning the model (used for validity check)
    speaker_name_prompts = ['Mitch McConnell', 'Harry Reid', 'Richard Durbin', 'Neil Abercrombie', 'Travis Childers', 'Gokberk Ozsoy']
    generated_text_logger.info('before fine-tuning')
    generate_text(net, tokenizer, speaker_name_prompts)
    generated_text_logger.info('-----')


    for epoch in range(cfg['epochs']):  # loop over dataset

        net.train()
        performance_logger.info(f'epoch: {epoch+1} / {cfg["epochs"]}')
        
        batch_perplexity_array=[]
        batch_loss_array=[]

        total_iterations = int(len(train_dataloader) / cfg['gradient_accumulations'])

        # training
        for batch_idx, batch_data in enumerate(train_dataloader): # loop over train batches
            
            batch_data = batch_data.to(device)

            # forward pass with mixed precision
            with autocast():
                loss,_ = net(batch_data)

            # backpropagation
            scaler.scale(loss / cfg['gradient_accumulations']).backward()

            # gradient descent with optimizer
            if (batch_idx + 1) % cfg['gradient_accumulations'] == 0:
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()

            # save batch metrics
            detached_loss = loss.detach().cpu()
            batch_loss_array.append(detached_loss.item())
            batch_perplexity_array.append(torch.exp(detached_loss).item())

            # print intermediate iterations during epoch
            if ((batch_idx + 1) / cfg['gradient_accumulations']) % cfg['print_iter_freq'] == 0:
                intermediate_batch_loss = np.mean(batch_loss_array[-cfg['gradient_accumulations']:])
                intermediate_batch_perplexity = np.mean(batch_perplexity_array[-cfg['gradient_accumulations']:])
                performance_logger.info(f'iter: {int((batch_idx + 1) / cfg["gradient_accumulations"])} / {total_iterations}, iter_loss: {intermediate_batch_loss:.4f}, iter_perplexity: {intermediate_batch_perplexity:.4f}')               
                #train_writer.add_scalar('iter_loss', intermediate_batch_loss, int((batch_idx + 1) / cfg["gradient_accumulations"]) + epoch*total_iterations)
                #train_writer.add_scalar('iter_perplexity', intermediate_batch_perplexity, int((batch_idx + 1) / cfg["gradient_accumulations"]) + epoch*total_iterations)

                # TEMPORARY: generate text after between iterations (used for validity check)
                generated_text_logger.info(f'iter: {int((batch_idx + 1) / cfg["gradient_accumulations"])} / {total_iterations}')
                generate_text(net, tokenizer, speaker_name_prompts)
                generated_text_logger.info('-----')

        # validation
        net.eval()
        with torch.no_grad():

            batch_perplexity_array_valid=[]
            batch_loss_array_valid=[]

            for _, valid_batch_data in enumerate(valid_dataloader): # loop over valid batches
                
                valid_batch_data = valid_batch_data.to(device)

                # forward pass with mixed precision
                with autocast():
                    val_loss,_ = net(valid_batch_data)

                # save batch metrics
                detached_val_loss = val_loss.detach().cpu()
                batch_loss_array_valid.append(detached_val_loss.item())
                batch_perplexity_array_valid.append(torch.exp(detached_val_loss).item())


            # generate speech with fine-tuned model
            generated_text_logger.info(f'epoch: {epoch+1} / {cfg["epochs"]}')
            generate_text(net, tokenizer, speaker_name_prompts)
            generated_text_logger.info('-----')
            

        # display metrics at end of epoch
        epoch_train_loss, epoch_train_perplexity = np.mean(batch_loss_array), np.mean(batch_perplexity_array)
        epoch_val_loss, epoch_val_perplexity = np.mean(batch_loss_array_valid), np.mean(batch_perplexity_array_valid)

        performance_logger.info(f'epoch: {epoch+1} / {cfg["epochs"]}, train_loss: {epoch_train_loss:.4f}, train_perplexity: {epoch_train_perplexity:.4f}, val_loss: {epoch_val_loss:.4f}, val_perplexity: {epoch_val_perplexity:.4f}\n')
        #train_writer.add_scalar('epoch_loss', epoch_train_loss, epoch)
        #train_writer.add_scalar('epoch_perplexity', epoch_train_perplexity, epoch)


        # save if best
        #if lowest_train_loss > epoch_train_loss:
        #    lowest_train_loss = epoch_train_loss
        # save for each epoch
        save_dict = {'epoch': epoch,
                'model_state_dict': net.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': epoch_train_loss}
        torch.save(save_dict, os.path.join(cfg['checkpoint_dir'],cfg['experiment_name']+'_epoch'+str(epoch)+'.pt'))
    
    
    return



if __name__ == '__main__':

    # load settings from config file
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, help="Path to a config file.")

    _args = parser.parse_args()

    with open(_args.config, "r") as f:
        cfg = yaml.safe_load(f)

    # create loggers
    performance_logger = logging.getLogger(cfg['experiment_name']+'_perf_logger')
    performance_logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s  - %(message)s','%d-%m-%Y %H:%M')
    perf_file_handler = logging.FileHandler(os.path.join(cfg['log_dir'],cfg['experiment_name']+'_performance'))
    perf_file_handler.setLevel(logging.INFO)
    perf_file_handler.setFormatter(formatter)
    performance_logger.addHandler(perf_file_handler)

    generated_text_logger = logging.getLogger(cfg['experiment_name']+'_gen_logger')
    generated_text_logger.setLevel(logging.INFO)
    formatter2 = logging.Formatter('%(message)s')
    gen_file_handler = logging.FileHandler(os.path.join(cfg['log_dir'],cfg['experiment_name']+'_generated_texts'))
    gen_file_handler.setLevel(logging.INFO)
    gen_file_handler.setFormatter(formatter2)
    generated_text_logger.addHandler(gen_file_handler)

    # print settings
    performance_logger.info(f'cfg: {cfg}')

    # setting device on GPU if available, else CPU
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    performance_logger.info(f'Using device: {device}')

    train(cfg, device)
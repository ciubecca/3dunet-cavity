import os

import torch
import torch.nn as nn
from tensorboardX import SummaryWriter
from torch.optim.lr_scheduler import ReduceLROnPlateau

from pytorch3dunet.datasets.utils import get_train_loaders
from pytorch3dunet.unet3d.losses import get_loss_criterion
from pytorch3dunet.unet3d.metrics import get_evaluation_metric
from pytorch3dunet.unet3d.model import get_model
from pytorch3dunet.unet3d.utils import profile, get_logger, get_tensorboard_formatter, create_sample_plotter, create_optimizer, \
    create_lr_scheduler, get_number_of_learnable_parameters
from . import utils

logger = get_logger('UNet3DTrainer')


def _create_trainer(config, model, optimizer, lr_scheduler, loss_criterion, eval_criterion, loaders, dry_run):
    assert 'trainer' in config, 'Could not find trainer configuration'
    trainer_config = config['trainer']

    resume = trainer_config.get('resume', None)
    pre_trained = trainer_config.get('pre_trained', None)

    # get tensorboard formatter
    tensorboard_formatter = get_tensorboard_formatter(trainer_config.pop('tensorboard_formatter', None))
    # get sample plotter
    sample_plotter = create_sample_plotter(trainer_config.pop('sample_plotter', None))

    if resume is not None:
        # continue training from a given checkpoint
        return UNet3DTrainer.from_checkpoint(model=model,
                                             optimizer=optimizer,
                                             lr_scheduler=lr_scheduler,
                                             loss_criterion=loss_criterion,
                                             eval_criterion=eval_criterion,
                                             loaders=loaders,
                                             tensorboard_formatter=tensorboard_formatter,
                                             sample_plotter=sample_plotter,
                                             **trainer_config)
    elif pre_trained is not None:
        # fine-tune a given pre-trained model
        return UNet3DTrainer.from_pretrained(model=model,
                                             optimizer=optimizer,
                                             lr_scheduler=lr_scheduler,
                                             loss_criterion=loss_criterion,
                                             eval_criterion=eval_criterion,
                                             tensorboard_formatter=tensorboard_formatter,
                                             sample_plotter=sample_plotter,
                                             device=config['device'],
                                             loaders=loaders,
                                             **trainer_config)
    else:
        # start training from scratch
        return UNet3DTrainer(model=model,
                             optimizer=optimizer,
                             lr_scheduler=lr_scheduler,
                             loss_criterion=loss_criterion,
                             eval_criterion=eval_criterion,
                             device=config['device'],
                             loaders=loaders,
                             tensorboard_formatter=tensorboard_formatter,
                             sample_plotter=sample_plotter,
                             dry_run=dry_run,
                             **trainer_config)


class UNet3DTrainerBuilder:
    @staticmethod
    def build(config):
        # Create the model
        config['model']['in_channels'] = len(config['loaders']['featurizer'])
        model = get_model(config['model'])
        # use DataParallel if more than 1 GPU available
        device = config['device']
        if torch.cuda.device_count() > 1 and not device.type == 'cpu':
            model = nn.DataParallel(model)
            logger.info(f'Using {torch.cuda.device_count()} GPUs for training')

        # put the model on GPUs
        logger.info(f"Sending the model to '{config['device']}'")
        model = model.to(device)

        # Log the number of learnable parameters
        logger.info(f'Number of learnable params {get_number_of_learnable_parameters(model)}')

        # Create loss criterion
        loss_criterion = get_loss_criterion(config)
        # Create evaluation metric
        eval_criterion = get_evaluation_metric(config)

        # Create data loaders
        loaders = get_train_loaders(config)

        # Create the optimizer
        optimizer = create_optimizer(config['optimizer'], model)

        # Create learning rate adjustment strategy
        lr_scheduler = create_lr_scheduler(config.get('lr_scheduler', None), optimizer)

        # Create model trainer
        trainer = _create_trainer(config, model=model, optimizer=optimizer, lr_scheduler=lr_scheduler,
                                  loss_criterion=loss_criterion, eval_criterion=eval_criterion, loaders=loaders,
                                  dry_run=config['dry_run'])

        return trainer


class UNet3DTrainer:
    """3D UNet trainer.

    Args:
        model (Unet3D): UNet 3D model to be trained
        optimizer (nn.optim.Optimizer): optimizer used for training
        lr_scheduler (torch.optim.lr_scheduler._LRScheduler): learning rate scheduler
            WARN: bear in mind that lr_scheduler.step() is invoked after every validation step
            (i.e. validate_after_iters) not after every epoch. So e.g. if one uses StepLR with step_size=30
            the learning rate will be adjusted after every 30 * validate_after_iters iterations.
        loss_criterion (callable): loss function
        eval_criterion (callable): used to compute training/validation metric (such as Dice, IoU, AP or Rand score)
            saving the best checkpoint is based on the result of this function on the validation set
        device (torch.device): device to train on
        loaders (dict): 'train' and 'val' loaders
        checkpoint_dir (string): dir for saving checkpoints and tensorboard logs
        max_num_epochs (int): maximum number of epochs
        max_num_iterations (int): maximum number of iterations
        validate_after_iters (int): validate after that many iterations
        log_after_iters (int): number of iterations before logging to tensorboard
        validate_iters (int): number of validation iterations, if None validate
            on the whole validation set
        eval_score_higher_is_better (bool): if True higher eval scores are considered better
        best_eval_score (float): best validation score so far (higher better)
        num_iterations (int): useful when loading the model from the checkpoint
        num_epoch (int): useful when loading the model from the checkpoint
        tensorboard_formatter (callable): converts a given batch of input/output/target image to a series of images
            that can be displayed in tensorboard
        sample_plotter (callable): saves sample inputs, network outputs and targets to a given directory
            during validation phase
        skip_train_validation (bool): if True eval_criterion is not evaluated on the training set (used mostly when
            evaluation is expensive)
    """

    def __init__(self, model, optimizer, lr_scheduler, loss_criterion,
                 eval_criterion, device, loaders, checkpoint_dir,
                 max_num_epochs=100, max_num_iterations=int(1e5),
                 validate_after_iters=100, log_after_iters=100,
                 validate_iters=None, num_iterations=1, num_epoch=0,
                 eval_score_higher_is_better=True, best_eval_score=None,
                 tensorboard_formatter=None, sample_plotter=None,
                 skip_train_validation=False,
                 dry_run=False, **kwargs):

        self.model = model
        self.optimizer = optimizer
        self.scheduler = lr_scheduler
        self.loss_criterion = loss_criterion
        self.eval_criterion = eval_criterion
        self.device = device
        self.loaders = loaders
        self.checkpoint_dir = checkpoint_dir
        self.max_num_epochs = max_num_epochs
        self.max_num_iterations = max_num_iterations
        self.validate_after_iters = validate_after_iters
        self.log_after_iters = log_after_iters
        self.validate_iters = validate_iters
        self.eval_score_higher_is_better = eval_score_higher_is_better
        self.dry_run = dry_run

        self.valLoaders = self.loaders['val'](seed=0)

        logger.info(model)
        logger.info(f'eval_score_higher_is_better: {eval_score_higher_is_better}')

        if best_eval_score is not None:
            self.best_eval_score = best_eval_score
        else:
            # initialize the best_eval_score
            if eval_score_higher_is_better:
                self.best_eval_score = float('-inf')
            else:
                self.best_eval_score = float('+inf')

        self.writer = SummaryWriter(log_dir=os.path.join(checkpoint_dir, 'logs'))

        assert tensorboard_formatter is not None, 'TensorboardFormatter must be provided'
        self.tensorboard_formatter = tensorboard_formatter
        self.sample_plotter = sample_plotter

        self.num_iterations = num_iterations
        self.num_epoch = num_epoch
        self.skip_train_validation = skip_train_validation

    @classmethod
    def from_checkpoint(cls, resume, model, optimizer, lr_scheduler, loss_criterion, eval_criterion, loaders,
                        tensorboard_formatter=None, sample_plotter=None, **kwargs):
        logger.info(f"Loading checkpoint '{resume}'...")
        state = utils.load_checkpoint(resume, model, optimizer)
        logger.info(
            f"Checkpoint loaded. Epoch: {state['epoch']}. Best val score: {state['best_eval_score']}. Num_iterations: {state['num_iterations']}")
        checkpoint_dir = os.path.split(resume)[0]
        return cls(model, optimizer, lr_scheduler,
                   loss_criterion, eval_criterion,
                   torch.device(state['device']),
                   loaders, checkpoint_dir,
                   eval_score_higher_is_better=state['eval_score_higher_is_better'],
                   best_eval_score=state['best_eval_score'],
                   num_iterations=state['num_iterations'],
                   num_epoch=state['epoch'],
                   max_num_epochs=state['max_num_epochs'],
                   max_num_iterations=state['max_num_iterations'],
                   validate_after_iters=state['validate_after_iters'],
                   log_after_iters=state['log_after_iters'],
                   validate_iters=state['validate_iters'],
                   skip_train_validation=state.get('skip_train_validation', False),
                   tensorboard_formatter=tensorboard_formatter,
                   sample_plotter=sample_plotter)

    @classmethod
    def from_pretrained(cls, pre_trained, model, optimizer, lr_scheduler, loss_criterion, eval_criterion,
                        device, loaders,
                        max_num_epochs=100, max_num_iterations=int(1e5),
                        validate_after_iters=100, log_after_iters=100,
                        validate_iters=None, num_iterations=1, num_epoch=0,
                        eval_score_higher_is_better=True, best_eval_score=None,
                        tensorboard_formatter=None, sample_plotter=None,
                        skip_train_validation=False, **kwargs):
        logger.info(f"Logging pre-trained model from '{pre_trained}'...")
        utils.load_checkpoint(pre_trained, model, None)
        if 'checkpoint_dir' not in kwargs:
            checkpoint_dir = os.path.split(pre_trained)[0]
        else:
            checkpoint_dir = kwargs.pop('checkpoint_dir')
        return cls(model, optimizer, lr_scheduler,
                   loss_criterion, eval_criterion,
                   device, loaders, checkpoint_dir,
                   eval_score_higher_is_better=eval_score_higher_is_better,
                   best_eval_score=best_eval_score,
                   num_iterations=num_iterations,
                   num_epoch=num_epoch,
                   max_num_epochs=max_num_epochs,
                   max_num_iterations=max_num_iterations,
                   validate_after_iters=validate_after_iters,
                   log_after_iters=log_after_iters,
                   validate_iters=validate_iters,
                   tensorboard_formatter=tensorboard_formatter,
                   sample_plotter=sample_plotter,
                   skip_train_validation=skip_train_validation)

    @profile
    def fit(self):
        for i in range(self.num_epoch, self.max_num_epochs):

            trainLoaders = self.loaders['train'](seed=i)

            # train for one epoch
            should_terminate = self.train(trainLoaders)

            if should_terminate:
                logger.info('Stopping criterion is satisfied. Finishing training')
                return

            self.num_epoch += 1
        logger.info(f"Reached maximum number of epochs: {self.max_num_epochs}. Finishing training...")

    @profile
    def train(self, trainLoaders):
        """Trains the model for 1 epoch.

        Returns:
            True if the training should be terminated immediately, False otherwise
        """
        train_losses = utils.RunningAverage()
        train_eval_scores = utils.RunningAverage()

        # sets the model in training mode
        self.model.train()

        for t in trainLoaders:
            logger.info(f'Training iteration [{self.num_iterations}/{self.max_num_iterations}]. '
                        f'Epoch [{self.num_epoch}/{self.max_num_epochs - 1}]')

            name, (input, target, weight) = self._split_training_batch(t)
            logger.debug(f'Input.shape: {input.shape}. target.shape: {target.shape}')
            logger.debug(f'Forward passing sample {name}. input.dtype: {input.dtype}, target.dtype: {target.dtype}')

            if input.dtype != torch.float32:
                raise ValueError("Not a float32")

            if self.dry_run:
                continue
            output, loss = self._forward_pass(input, target, weight)

            train_losses.update(loss.item(), self._batch_size(input))

            # compute gradients and update parameters
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            if self.num_iterations % self.validate_after_iters == 0:
                # set the model in eval mode
                self.model.eval()
                # evaluate on validation set
                eval_score = self.validate()
                # set the model back to training mode
                self.model.train()

                # adjust learning rate if necessary
                if isinstance(self.scheduler, ReduceLROnPlateau):
                    self.scheduler.step(eval_score)
                else:
                    self.scheduler.step()
                # log current learning rate in tensorboard
                self._log_lr()
                # remember best validation metric
                is_best = self._is_best_eval_score(eval_score)

                # save checkpoint
                self._save_checkpoint(is_best)

            if self.num_iterations % self.log_after_iters == 0:
                # if model contains final_activation layer for normalizing logits apply it, otherwise both
                # the evaluation metric as well as images in tensorboard will be incorrectly computed
                if hasattr(self.model, 'final_activation') and self.model.final_activation is not None:
                    output = self.model.final_activation(output)

                # compute eval criterion
                if not self.skip_train_validation:
                    eval_score = self.eval_criterion(output, target)
                    train_eval_scores.update(eval_score.item(), self._batch_size(input))

                # log stats, params and images
                logger.info(
                    f'Training stats. Loss: {train_losses.avg}. Evaluation score: {train_eval_scores.avg}')
                self._log_stats('train', train_losses.avg, train_eval_scores.avg)
                self._log_params()
                self._log_images(input, target, output, 'train_')

            if self.should_stop():
                return True

            self.num_iterations += 1

        return False

    def should_stop(self):
        """
        Training will terminate if maximum number of iterations is exceeded or the learning rate drops below
        some predefined threshold (1e-6 in our case)
        """
        if self.max_num_iterations < self.num_iterations:
            logger.info(f'Maximum number of iterations {self.max_num_iterations} exceeded.')
            return True

        min_lr = 1e-6
        lr = self.optimizer.param_groups[0]['lr']
        if lr < min_lr:
            logger.info(f'Learning rate below the minimum {min_lr}.')
            return True

        return False

    def validate(self):
        logger.info('Validating...')

        val_losses = utils.RunningAverage()
        val_scores = utils.RunningAverage()

        if self.sample_plotter is not None:
            self.sample_plotter.update_current_dir()

        with torch.no_grad():

            for i, t in enumerate(self.valLoaders):
                logger.info(f'Validation iteration {i}')

                name, (input, target, weight) = self._split_training_batch(t)

                if self.dry_run:
                    continue

                output, loss = self._forward_pass(input, target, weight)
                val_losses.update(loss.item(), self._batch_size(input))

                # if model contains final_activation layer for normalizing logits apply it, otherwise
                # the evaluation metric will be incorrectly computed
                if hasattr(self.model, 'final_activation') and self.model.final_activation is not None:
                    output = self.model.final_activation(output)

                if i % 100 == 0:
                    self._log_images(input, target, output, 'val_')

                # mmin,mmax = output.min(), output.max()
                eval_score = self.eval_criterion(output, target)
                val_scores.update(eval_score.item(), self._batch_size(input))

                if self.sample_plotter is not None:
                    self.sample_plotter(i, input, output, target, 'val')

                if self.validate_iters is not None and self.validate_iters <= i:
                    # stop validation
                    break

            self._log_stats('val', val_losses.avg, val_scores.avg)
            logger.info(f'Validation finished. Loss: {val_losses.avg}. Evaluation score: {val_scores.avg}')
            return val_scores.avg

    @profile
    def _split_training_batch(self, t):
        name, t = t

        def _move_to_device(input):
            if isinstance(input, tuple) or isinstance(input, list):
                return tuple([_move_to_device(x) for x in input])
            else:
                return input.to(self.device)

        t = _move_to_device(t)
        weight = None
        if len(t) == 2:
            input, target = t
        else:
            input, target, weight = t
        return name, (input, target, weight)

    def _forward_pass(self, input, target, weight=None):
        # forward pass
        output = self.model(input)

        # compute the loss
        # assert ((output <= 1.0) & (output >= 0.0)).all()
        if weight is None:
            loss = self.loss_criterion(output, target)
        else:
            loss = self.loss_criterion(output, target, weight)

        return output, loss

    def _is_best_eval_score(self, eval_score):
        if self.eval_score_higher_is_better:
            is_best = eval_score > self.best_eval_score
        else:
            is_best = eval_score < self.best_eval_score

        if is_best:
            logger.info(f'Saving new best evaluation metric: {eval_score}')
            self.best_eval_score = eval_score

        return is_best

    def _save_checkpoint(self, is_best):
        # remove `module` prefix from layer names when using `nn.DataParallel`
        # see: https://discuss.pytorch.org/t/solved-keyerror-unexpected-key-module-encoder-embedding-weight-in-state-dict/1686/20
        if isinstance(self.model, nn.DataParallel):
            state_dict = self.model.module.state_dict()
        else:
            state_dict = self.model.state_dict()

        utils.save_checkpoint({
            'epoch': self.num_epoch + 1,
            'num_iterations': self.num_iterations,
            'model_state_dict': state_dict,
            'best_eval_score': self.best_eval_score,
            'eval_score_higher_is_better': self.eval_score_higher_is_better,
            'optimizer_state_dict': self.optimizer.state_dict(),
            'device': str(self.device),
            'max_num_epochs': self.max_num_epochs,
            'max_num_iterations': self.max_num_iterations,
            'validate_after_iters': self.validate_after_iters,
            'log_after_iters': self.log_after_iters,
            'validate_iters': self.validate_iters,
            'skip_train_validation': self.skip_train_validation
        }, is_best, checkpoint_dir=self.checkpoint_dir,
            logger=logger)

    def _log_lr(self):
        lr = self.optimizer.param_groups[0]['lr']
        self.writer.add_scalar('learning_rate', lr, self.num_iterations)

    def _log_stats(self, phase, loss_avg, eval_score_avg):
        tag_value = {
            f'{phase}_loss_avg': loss_avg,
            f'{phase}_eval_score_avg': eval_score_avg
        }

        for tag, value in tag_value.items():
            self.writer.add_scalar(tag, value, self.num_iterations)

    def _log_params(self):
        logger.info('Logging model parameters and gradients')
        for name, value in self.model.named_parameters():
            self.writer.add_histogram(name, value.data.cpu().numpy(), self.num_iterations)
            self.writer.add_histogram(name + '/grad', value.grad.data.cpu().numpy(), self.num_iterations)

    def _log_images(self, input, target, prediction, prefix=''):
        inputs_map = {
            'inputs': input,
            'targets': target,
            'predictions': prediction
        }
        img_sources = {}
        for name, batch in inputs_map.items():
            if isinstance(batch, list) or isinstance(batch, tuple):
                for i, b in enumerate(batch):
                    img_sources[f'{name}{i}'] = b.data.cpu().numpy()
            else:
                img_sources[name] = batch.data.cpu().numpy()

        for name, batch in img_sources.items():
            for tag, image in self.tensorboard_formatter(name, batch):
                self.writer.add_image(prefix + tag, image, self.num_iterations, dataformats='CHW')

    @staticmethod
    def _batch_size(input):
        if isinstance(input, list) or isinstance(input, tuple):
            return input[0].size(0)
        else:
            return input.size(0)

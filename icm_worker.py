#!/usr/bin/env python
import tensorflow as tf
import argparse
import logging
import sys, signal
import time
import os
from icm.a3c import A3C
from icm.constants import constants
import distutils.version
use_tf12_api = distutils.version.LooseVersion(tf.VERSION) >= distutils.version.LooseVersion('0.12.0')

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Disables write_meta_graph argument, which freezes entire process and is mostly useless.
class FastSaver(tf.train.Saver):
    def save(self, sess, save_path, global_step=None, latest_filename=None,
             meta_graph_suffix="meta", write_meta_graph=True):
        super(FastSaver, self).save(sess, save_path, global_step, latest_filename,
                                    meta_graph_suffix, False)

def run(args, server):
    if args.env_id in ["OverCooked"]:
        import overcooked
        print(args.save_dir)
        args.run_overcooked = False
        env = overcooked.OverCooked(
            args = args,
            obs_size = 42,
        )
    else:
        from icm.envs import create_env
        env = create_env(args.env_id, client_id=str(args.task), remotes=args.remotes, envWrap=args.envWrap, designHead=args.designHead,
                        noLifeReward=args.noLifeReward)
    trainer = A3C(env, args.task, args.visualise, args.unsup, args.envWrap, args.designHead, args.noReward)

    # logging
    if args.task == 0:
        with open(args.save_dir + '/log.txt', 'w') as fid:
            for key, val in constants.items():
                fid.write('%s: %s\n'%(str(key), str(val)))
            fid.write('designHead: %s\n'%args.designHead)
            fid.write('input observation: %s\n'%str(env.observation_space.shape))
            fid.write('env name: %s\n'%str(args.env_id))
            fid.write('unsup method type: %s\n'%str(args.unsup))

    # Variable names that start with "local" are not saved in checkpoints.
    if use_tf12_api:
        variables_to_save = [v for v in tf.global_variables() if not v.name.startswith("local")]
        init_op = tf.variables_initializer(variables_to_save)
        init_all_op = tf.global_variables_initializer()
    else:
        variables_to_save = [v for v in tf.all_variables() if not v.name.startswith("local")]
        init_op = tf.initialize_variables(variables_to_save)
        init_all_op = tf.initialize_all_variables()
    saver = FastSaver(variables_to_save)
    if args.pretrain is not None:
        variables_to_restore = [v for v in tf.trainable_variables() if not v.name.startswith("local")]
        pretrain_saver = FastSaver(variables_to_restore)

    var_list = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, tf.get_variable_scope().name)
    logger.info('Trainable vars:')
    for v in var_list:
        logger.info('  %s %s', v.name, v.get_shape())

    def init_fn(ses):
        logger.info("Initializing all parameters.")
        ses.run(init_all_op)
        if args.pretrain is not None:
            pretrain = tf.train.latest_checkpoint(args.pretrain)
            logger.info("==> Restoring from given pretrained checkpoint.")
            logger.info("    Pretraining address: %s", pretrain)
            pretrain_saver.restore(ses, pretrain)
            logger.info("==> Done restoring model! Restored %d variables.", len(variables_to_restore))

    config = tf.ConfigProto(device_filters=["/job:ps", "/job:worker/task:{}/cpu:0".format(args.task)])
    logdir = os.path.join(args.save_dir, 'train')

    if use_tf12_api:
        summary_writer = tf.summary.FileWriter(logdir + "_%d" % args.task)
    else:
        summary_writer = tf.train.SummaryWriter(logdir + "_%d" % args.task)

    logger.info("Events directory: %s_%s", logdir, args.task)
    sv = tf.train.Supervisor(is_chief=(args.task == 0),
                             logdir=logdir,
                             saver=saver,
                             summary_op=None,
                             init_op=init_op,
                             init_fn=init_fn,
                             summary_writer=summary_writer,
                             ready_op=tf.report_uninitialized_variables(variables_to_save),
                             global_step=trainer.global_step,
                             save_model_secs=30,
                             save_summaries_secs=30)

    num_global_steps = constants['MAX_GLOBAL_STEPS']

    logger.info(
        "Starting session. If this hangs, we're mostly likely waiting to connect to the parameter server. " +
        "One common cause is that the parameter server DNS name isn't resolving yet, or is misspecified.")
    with sv.managed_session(server.target, config=config) as sess, sess.as_default():
        # Workaround for FailedPreconditionError
        # see: https://github.com/openai/universe-starter-agent/issues/44 and 31
        sess.run(trainer.sync)

        trainer.start(sess, summary_writer)
        global_step = sess.run(trainer.global_step)
        logger.info("Starting training at gobal_step=%d", global_step)
        while not sv.should_stop() and (not num_global_steps or global_step < num_global_steps):
            trainer.process(sess)
            global_step = sess.run(trainer.global_step)

    # Ask for all the services to stop.
    sv.stop()
    logger.info('reached %s steps. worker stopped.', global_step)

def cluster_spec(num_workers, num_ps, port=12222):
    """
More tensorflow setup for data parallelism
"""
    cluster = {}

    all_ps = []
    host = '127.0.0.1'
    for _ in range(num_ps):
        all_ps.append('{}:{}'.format(host, port))
        port += 1
    cluster['ps'] = all_ps

    all_workers = []
    for _ in range(num_workers):
        all_workers.append('{}:{}'.format(host, port))
        port += 1
    cluster['worker'] = all_workers
    return cluster

def main(_):
    """
Setting up Tensorflow for data parallel work
"""

    parser = argparse.ArgumentParser(description=None)
    parser.add_argument('-v', '--verbose', action='count', dest='verbosity', default=0, help='Set verbosity.')
    parser.add_argument('--task', default=0, type=int, help='Task index')
    parser.add_argument('--job-name', default="worker", help='worker or ps')
    parser.add_argument('--num-workers', default=1, type=int, help='Number of workers')
    parser.add_argument('--log-dir', default="tmp/doom", help='Log directory path')
    parser.add_argument('--env-id', default="doom", help='Environment id')
    parser.add_argument('-r', '--remotes', default=None,
                        help='References to environments to create (e.g. -r 20), '
                             'or the address of pre-existing VNC servers and '
                             'rewarders to use (e.g. -r vnc://localhost:5900+15900,vnc://localhost:5901+15901)')
    parser.add_argument('--visualise', action='store_true',
                        help="Visualise the gym environment by running env.render() between each timestep")
    parser.add_argument('--envWrap', action='store_true',
                        help="Preprocess input in env_wrapper (no change in input size or network)")
    parser.add_argument('--designHead', type=str, default='universe',
                        help="Network deign head: nips or nature or doom or universe(default)")
    parser.add_argument('--unsup', type=str, default=None,
                        help="Unsup. exploration mode: action or state or stateAenc or None")
    parser.add_argument('--noReward', action='store_true', help="Remove all extrinsic reward")
    parser.add_argument('--noLifeReward', action='store_true',
                        help="Remove all negative reward (in doom: it is living reward)")
    parser.add_argument('--psPort', default=12222, type=int, help='Port number for parameter server')
    parser.add_argument('--delay', default=0, type=int, help='delay start by these many seconds')
    parser.add_argument('--pretrain', type=str, default=None, help="Checkpoint dir (generally ..../train/) to load from.")
    parser.add_argument('--obs-type', type=str, default='image',
                        help='observation type: image or ram' )
    parser.add_argument('--env-name', default='PongNoFrameskip-v4',
                        help='environment to train on')
    parser.add_argument('--reward-level', type=int, default=2,
                        help='level of reward in games like OverCooked')
    parser.add_argument('--use-fake-reward-bounty', action='store_true',
                        help='if use fake reward bounty')
    parser.add_argument('--reset-leg', action='store_true',
                        help='if reset four legs after four steps')
    parser.add_argument('--add-goal-color', action='store_true',
                        help='if add area color when get the goal')
    parser.add_argument('--log-behavior-interval', type=int, default=10,
                        help='log behavior every x minutes')
    parser.add_argument('--act-deterministically', action='store_true',
                        help='if act deterministically when interactiong')
    parser.add_argument('--aux', type=str, default='',
                        help='some aux information you may want to record along with this run')
    parser.add_argument('--save-dir', default='../results/',
                        help='directory to save agent logs')
    parser.add_argument('--exp', type=str,
                        help='exp')

    args = parser.parse_args()

    '''basic save path'''
    args.save_dir = os.path.join(args.save_dir, args.exp)
    args.save_dir = os.path.join(args.save_dir, 'icm')

    '''environment details'''
    args.save_dir = os.path.join(args.save_dir, 'o_t-{}'.format(args.obs_type))
    args.save_dir = os.path.join(args.save_dir, 'e_n-{}'.format(args.env_name))
    if args.env_name in ['OverCooked']:
        args.save_dir = os.path.join(args.save_dir, 'r_l-{}'.format(args.reward_level))
        args.save_dir = os.path.join(args.save_dir, 'u_f_r_b-{}'.format(args.use_fake_reward_bounty))
        args.save_dir = os.path.join(args.save_dir, 'r_lg-{}'.format(args.reset_leg))
        args.save_dir = os.path.join(args.save_dir, 'a_g_c-{}'.format(args.add_goal_color))

    args.save_dir = os.path.join(args.save_dir, 'a-{}'.format(args.aux))

    spec = cluster_spec(args.num_workers, 1, args.psPort)
    cluster = tf.train.ClusterSpec(spec).as_cluster_def()

    def shutdown(signal, frame):
        logger.warn('Received signal %s: exiting', signal)
        sys.exit(128+signal)
    signal.signal(signal.SIGHUP, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if args.job_name == "worker":
        server = tf.train.Server(cluster, job_name="worker", task_index=args.task,
                                 config=tf.ConfigProto(intra_op_parallelism_threads=1, inter_op_parallelism_threads=2))
        if args.delay > 0:
            print('Startup delay in worker: {}s'.format(args.delay))
            time.sleep(args.delay)
            print('.. wait over !')
        run(args, server)
    else:
        server = tf.train.Server(cluster, job_name="ps", task_index=args.task,
                                 config=tf.ConfigProto(device_filters=["/job:ps"]))
        while True:
            time.sleep(1000)

if __name__ == "__main__":
    tf.app.run()
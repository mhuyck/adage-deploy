"""Fab tasks to provision an Adage server.

Use these only once to setup an ubuntu server. For day to day usage and 
development you should use the adage-server fabfile. Typing "fab" by itself will 
invoke the default 'deploy' command and attempt a full deploy from scratch.

If the private key for your aws ubuntu instance is kept in 
~/.ssh/aws_ubuntu.pem, invoking adage_server.setup_ec2_conn() will attempt to use 
that for connections. (You will see warning messages if this file is missing.)

A default host is also set up in the adage_server.setup_ec2_conn() task, but 
could be specified via the fab -H switch for a more traditional fab usage.

To launch and configure a new ec2 instance, make sure your database 
configuration (in adage-server/adage/adage/config.py under 
TEST_CONFIG['databases']) does not conflict with a running instance, and use: 
> fab
"""

# TODO: enhance with AWS API to automate creating an instance for databases (see: https://boto3.readthedocs.org/en/latest/reference/services/rds.html#RDS.Client.create_db_instance)
# TODO: simulate an ec2 instance on VMware for development (see: http://askubuntu.com/questions/153486/how-do-i-boot-ubuntu-cloud-images-in-vmware and https://cloud-images.ubuntu.com/trusty/current/)

from __future__ import with_statement
import logging
import os, sys
import pprint
from boto3.session import Session
from fabric.api import put, get, run, sudo, execute, reboot
from fabric.api import env, local, settings, hide, abort, task, runs_once
from fabric.contrib.console import confirm

# Now we have to import the fabfile we use for adage-server routine deployment. We
# depend upon some of the commands from that codebase so we can allow its configuration
# to remain self-contained while not repeating code we need to use here at deployment time.
# To achieve this, we carefully manipulate the system path to import the fabfile from the 
# adage-server directory. Note that in order to access the fabric commands without bashing
# our heads against Python namespace conflicts we have must keep the fabric commands for 
# one or the other of these deployment scripts in a file named something *other* than 
# fabfile.py. We have opted to place the adage-server deployment scripts in a fabfile/
# package. Currently, everything resides in one large module named fabfile/adage-server.py, 
# but this structure will allow us to break up the deployment scripts into smaller modules
# in the future, if we choose.
# BASE_DIR is equivalent to a relative path of ../../adage-server/
BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'adage-server')
if BASE_DIR not in sys.path:
    sys.path.insert(0, os.path.join(BASE_DIR, 'fabfile'))
# CONFIG_DIR is equivalent to a path of BASE_DIR/adage/adage/
CONFIG_DIR = os.path.join(BASE_DIR, 'adage', 'adage')
if CONFIG_DIR not in sys.path:
    sys.path.append(CONFIG_DIR)
# Now, finally, we can go ahead and import what we want.
import adage_server
import config

# Choose the configuration to use for the rest of this deployment from config.py
CONFIG = config.DEPLOY_TEST_CONFIG


@task
def print_config():
    """
    Show what configuration we are currently using
    NOTE: using this command will display passwords and other secrets
    """
    pprint.PrettyPrinter().pprint(CONFIG)


@task(alias='ec2-list')
def list_ec2_instances():
    """
    Show a list of all available ec2 instances
    """
    s = Session(**dict((k,v) for k, v in config.AWS_CONFIG.items() \
        if k in ('aws_access_key_id', 'aws_secret_access_key', 'region_name')))
    ec2 = s.resource('ec2')
    for i in ec2.instances.all():
        print("{0}: state: {1}\n\tdns: {2}\n\timage: {3}\n\tlaunched: {4}".format( \
            i.id, i.state, i.public_dns_name, i.image_id, i.launch_time))


@task(alias='ec2-new')
def launch_ec2_instance():
    """
    Launch a new ec2 instance, get its IP address and 
    reset env.user and env.hosts to point to just that new instance
    
    (for boto documentation, see: 
    https://boto3.readthedocs.org/en/latest/reference/services/ec2.html#EC2.ServiceResource.create_instances)
    """
    
    s = Session(**dict((k,v) for k, v in config.AWS_CONFIG.items() \
        if k in ('aws_access_key_id', 'aws_secret_access_key', 'region_name')))
    ec2 = s.resource('ec2')
    
    print("Launching new EC2 instance...")
    inst = ec2.create_instances(**config.AWS_CONFIG['ec2_params'])
    inst[0].wait_until_running()
    # override the current env.hosts since we are launching a new instance
    # NOTE: this seems like a convoluted way to get the IP address, but it works
    env.hosts = [i.public_ip_address for i in ec2.instances.all() if i.id == inst[0].id]
    env.user = 'ubuntu'
    print("New instance launching at: {0}".format(env.hosts[0]))
    # this is a little hackish, but it ensures the next command will not timeout because
    # the server hasn't actually finished coming online yet...
    print("Waiting for instance to come online...")
    # TODO: test this!
    with settings(hide('running'), warn_only=True):
        execute(reboot, command='hostname', hosts=[ 'ubuntu@' + h for h in env.hosts ])
    print("New instance is now running at: {0}".format(env.hosts[0]))


@task
def enable_unattended_updates():
    """
    Tell ubuntu to install upgrades automatically.

    This enables automatic and unattended upgrades. This also allows automatic
    reboots if required.
    """
    put('files/upgrade/20auto-upgrades', '/etc/apt/apt.conf.d/20auto-upgrades', use_sudo=True)
    put('files/upgrade/50unattended-upgrades', '/etc/apt/apt.conf.d/50unattended-upgrades', use_sudo=True)


def _install_elasticsearch():
    """
    Install ElasticSearch packages + JRE.

    Add the elastic search repo and key, and the install the ElasticSearch
    package and dependencies.
    """
    run('wget -qO - http://packages.elasticsearch.org/GPG-KEY-elasticsearch | sudo apt-key add -')
    run("""echo 'deb http://packages.elasticsearch.org/elasticsearch/1.4/debian stable main
' | sudo tee -a /etc/apt/sources.list""")
    sudo('apt-get update')
    sudo('apt-get -y -q install elasticsearch openjdk-7-jre')
    # this plugin lets us use 'network.host: _ec2_' in the config below
    run('cd /usr/share/elasticsearch; sudo bin/plugin install elasticsearch/elasticsearch-cloud-aws/2.4.2')


def _install_python_deps():
    """
    Install python and the packages required for development.

    Install mercurial, pip, distribute, and packages needed to build python
    libraries by pip.
    """
    sudo('apt-get -y -q install python python-dev mercurial git python-distribute python-pip python-virtualenv')


def _install_postgres():
    """
    Install postgres client packages.

    Install the packages needed to build postgres client utilities via pip.
    The repository for Ubuntu 14.04 (trusty) does not have a new enough version to be compatible
    with the AWS-installed copy of PostgreSQL (9.4.1), so we configure the PostgreSQL apt 
    repository and key and *then* do our install.
    """
    run("""echo 'deb http://apt.postgresql.org/pub/repos/apt/ trusty-pgdg main
' | sudo tee -a /etc/apt/sources.list.d/pgdg.list""")
    run('wget -qO - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -')
    sudo('apt-get update')
    sudo('apt-get -y -q install postgresql-common libpq-dev postgresql-client')


@task(alias='sys')
def install_system_packages():
    """
    Install all system packages required for Adage.

    Install all python, postgres, elasticsearch, and other packages required to deploy and manage
    an Adage instance.
    """
    sudo('apt-get update')
    execute(_install_elasticsearch)
    execute(_install_python_deps)
    execute(_install_postgres)
    sudo('apt-get -y -q install nodejs-legacy build-essential nginx npm supervisor')
    sudo('npm -g install grunt-cli karma bower')


@task
def setup_elasticsearch():
    """
    Configure ElasticSearch.

    Make a location for the search index, and configure the server to allow connections from
    localhost.
    """
    sudo('mkdir /var/elastic')
    sudo('chown -R elasticsearch:elasticsearch /var/elastic')
    sudo('sysctl -w vm.max_map_count=262144')
    run("echo 'ES_HEAP_SIZE=512m' | sudo tee -a /etc/environment")
    sudo('update-rc.d elasticsearch defaults 95 10')

    # elasticsearch should only look at this host. Create config files.
    # setting network.host per https://www.elastic.co/guide/en/elasticsearch/reference/current/modules-network.html since we were getting the same discovery errors in our log as HansPeterSloot at this thread <https://groups.google.com/forum/#!msg/elasticsearch/aVeipLZ5s0c/VXgQerDDuRYJ> (requires installation of https://github.com/elastic/elasticsearch-cloud-aws as we have done above)
#     run("""echo 'network.host: _ec2_
# script.disable_dynamic: true
    run("""echo 'script.disable_dynamic: true
bootstrap.mlockall: true
path.data: /var/elastic
path.logs: /var/log/elasticsearch
cluster.name: adagesearch
' | sudo tee -a /etc/elasticsearch/elasticsearch.yml""")
    # note: must have pty=False below for init script to work properly. See: <http://docs.fabfile.org/en/1.6/faq.html>
    sudo('/etc/init.d/elasticsearch restart', pty=False)


@task
def create_adage_user():
    """
    Create an adage user.

    Before running this command, make sure that you have created a file named
    authorized_keys in this directory that contains the public keys for people that
    will need to be able to access this instance of Adage as a user "adage".
    """
    logging.info("\nChecking for a non-empty copy of authorized_keys to upload for access to server...")
    local("test -s authorized_keys")
    sudo('adduser adage --disabled-password --gecos "&"')
    sudo('mkdir /home/adage/.ssh', user="adage")
    sudo('chmod 700 /home/adage/.ssh')
    put('authorized_keys', '/home/adage/.ssh/', use_sudo=True, mode=0600)
    sudo('chown adage:adage /home/adage/.ssh/authorized_keys')


@task
def setup_database():
    """
    Configure default AWS PostgreSQL setup with a login role for this web server
    """
    ## create .pgpass in /home/ubuntu with master db user info
    run("touch /home/ubuntu/.pgpass")
    run("chmod 600 /home/ubuntu/.pgpass")
    run("echo '{HOST}:{PORT}:{NAME}:{USER}:{PASSWORD}' >> /home/ubuntu/.pgpass".format( \
        **CONFIG['dbmaster']))
    
    sqlstr = """echo "create role {USER} with login createdb;
alter role {USER} with password '{PASSWORD}' valid until 'infinity';
grant {USER} to {MASTER_USER};
set role {USER};
create database {NAME};
" """.format(MASTER_USER=CONFIG['dbmaster']['USER'], **CONFIG['databases']['default'])
    run(sqlstr + ' | psql --host={HOST} --dbname={NAME} --username={USER} --no-password'.format( \
        **CONFIG['dbmaster']))
    
    ## create .pgpass in /home/adage with adage user info
    sudo("touch /home/adage/.pgpass", user="adage")
    sudo("chmod 600 /home/adage/.pgpass", user="adage")
    sudo("echo '{HOST}:{PORT}:{NAME}:{USER}:{PASSWORD}' >> /home/adage/.pgpass".format( \
        **CONFIG['databases']['default']), user="adage")


@task(alias='cdk')
def create_deploy_keys():
    """
    Create deployment keys.

    This command will create deployment keys on the remote server and download the
    public key as deploy_rsa.pub. Add this deployment key to bitbucket to be able
    to clone the mercurial repositories.
    """
    sudo("cd /home/adage/.ssh; ssh-keygen -f id_rsa -t rsa -N ''", user="adage")
    get('/home/adage/.ssh/id_rsa.pub', 'deploy_rsa.pub', use_sudo=True)
    local('cat deploy_rsa.pub')
    if not confirm("""A deployment key has been saved in deploy_rsa.pub (displayed above). 
Please upload this key to the adage repositories at 
https://bitbucket.org/greenelab/adage-server/admin/deploy-keys/ *and* 
https://bitbucket.org/greenelab/greenelab.bitbucket.org/admin/deploy-keys/ *and* 
https://bitbucket.org/greenelab/get_pseudomonas/admin/deploy-keys/ , following the instructions 
at https://confluence.atlassian.com/display/BITBUCKET/Use+deployment+keys now.
Ready to proceed?"""):
        abort("Okay, you're not ready. Sorry, but resuming is left as an exercise for the user.")


@task(alias='ca')
def clone_adage_repo():
    """
    Clone the Adage and the greenelab.bitbucket.org repositories.

    This command clones the adage repository from bitbucket into
    /home/adage/adage-server and the greenelab.bitbucket.org repository into
    /home/adage/greenelab. The adage repository is the location where the python
    code for the server is stored and the greenelab repository contains extra
    static files to be served outside of the adage application. It also downloads
    *just* the get_pseudo_sdrf.py file from the get_pseudomonas repository for
    bootstrapping.
    """
    sudo('hg clone ssh://hg@bitbucket.org/greenelab/adage-server /home/adage/adage-server', user="adage")
    sudo('hg clone ssh://hg@bitbucket.org/greenelab/greenelab.bitbucket.org /home/adage/greenelab', user="adage")
    ## this method is simpler but requires using a password, so it's less desirable --> run('wget -q https://bitbucket.org/greenelab/get_pseudomonas/raw/281f4fe00240e3effb4e5bc9a516e8a3716b9ede/get_pseudo_sdrf.py')
    sudo('hg clone --noupdate ssh://hg@bitbucket.org/greenelab/get_pseudomonas /home/adage/temp', user="adage")
    sudo('hg cat /home/adage/temp/get_pseudo_sdrf.py --rev tip -o "/home/adage/%s"', user="adage")
    sudo('rm -rf /home/adage/temp', user="adage")


@task
def setup_nginx():
    """
    Setup nginx.

    This command will remove the default nginx site, and put a configuration file
    for adage into the sites-enabled folder.
    """
    sudo('rm -f /etc/nginx/sites-enabled/default')
    put('files/nginx/adage-nginx.conf', '/etc/nginx/sites-enabled/', use_sudo=True)
    sudo('/etc/init.d/nginx restart')


@task
def setup_virtualenv():
    """
    Setup Python Virtual Envrionment.

    This command will create a virtual environment for Adage in
    /home/adage/.virtualenvs. This is the virtualenv that will contain the python
    packages that are pip installed from Adage's requirements.txt
    """
    sudo('mkdir -p /home/adage/.virtualenvs', user='adage')
    sudo('virtualenv /home/adage/.virtualenvs/adage', user='adage')


@task
def setup_supervisor():
    """
    Setup supervisor.

    Supervisor allows us to control gunicorn instances of Adage. gunicorn can be
    installed in the virtualenv, and the "adage" user can restart the server
    without requiring unrestricted sudo.
    """
    put('files/supervisord/adage_super.conf', '/etc/supervisor/conf.d/adage_super.conf', use_sudo=True)
    sudo('sudo /etc/init.d/supervisor restart')


@task
def setup_sudo_restart_super():
    """
    Allow the adage user to restart Supervisor.

    Create a supervisor group, add adage to it, upload a sudo configuration that
    allows the adage user to perform the restart procedure for the adage server.
    """
    put('files/supervisord/super_sudo', '/etc/sudoers.d/super_sudo', use_sudo=True, mode=0440)
    sudo('chown root:root /etc/sudoers.d/super_sudo')
    sudo('sudo /etc/init.d/supervisor restart')


@task
def configure_system():
    """
    Configure all base system setup tasks we can do before setting up the adage user
    """
    enable_unattended_updates()
    install_system_packages()
    setup_elasticsearch()


@task
def configure_adage():
    """
    Create the adage user and complete all user-dependent configuration
    """
    # create the adage user; make sure authorized_keys has been created by this point.
    create_adage_user()
    
    # create a database for this instance
    setup_database()
    
    # create the deployment key and retrieve it -- be prepared to enter a password for your ssh key
    # also let it save to the default location -- we count on that for the download.
    create_deploy_keys()
    
    # you need to have put the adage deployment key on the bitbucket repo before this step.
    clone_adage_repo()
    
    # you need to have setup the configuration (e.g. correct domain name, etc) for the
    # adage-nginx.conf file before running this step.
    setup_nginx()
    
    # create the virtualenv that adage uses
    setup_virtualenv()
    
    # setup supervisord -- you can configure the parameters for gunicorn but the ones that
    # exist are probably somewhat reasonablish.
    setup_supervisor()
    
    # allow the adage user to have permissions to restart adage (e.g. the gunicorn process)
    # via supervisor.
    setup_sudo_restart_super()


@task(default=True)
def deploy():
    """
    Execute a complete deployment to provision a new adage server (replaces steps.sh)
    """
    # execute(adage_server.load_aws_key, CONFIG['aws_ubuntu_key'])
    execute(adage_server.setup_ec2_conn, use_config=CONFIG)
    execute(launch_ec2_instance)
    hostlist = [ 'ubuntu@' + h for h in env.hosts ]
    execute(configure_system, hosts=hostlist)
    print("rebooting...")
    execute(reboot, wait=70, hosts=hostlist)
    execute(configure_adage, hosts=hostlist)
    hostlist = [ 'adage@' + h for h in env.hosts ]
    # hostlist=['adage@52.2.135.106']
    print("hosts=%s" % hostlist)
    execute(adage_server.setup_ec2_conn, hosts=hostlist)    # allow to default to adage_server CONFIG
    execute(adage_server.deploy, hosts=hostlist)

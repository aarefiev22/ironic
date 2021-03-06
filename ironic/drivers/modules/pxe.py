# Copyright 2013 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""
PXE Boot Interface
"""

import os
import shutil

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import fileutils

from ironic.common import boot_devices
from ironic.common import dhcp_factory
from ironic.common import exception
from ironic.common.glance_service import service_utils
from ironic.common.i18n import _
from ironic.common.i18n import _LE
from ironic.common.i18n import _LW
from ironic.common import image_service as service
from ironic.common import paths
from ironic.common import pxe_utils
from ironic.common import states
from ironic.common import utils
from ironic.drivers import base
from ironic.drivers.modules import agent
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules import image_cache
from ironic.drivers import utils as driver_utils


pxe_opts = [
    cfg.StrOpt('pxe_config_template',
               default=paths.basedir_def(
                   'drivers/modules/pxe_config.template'),
               help=_('On ironic-conductor node, template file for PXE '
                      'configuration.')),
    cfg.StrOpt('uefi_pxe_config_template',
               default=paths.basedir_def(
                   'drivers/modules/elilo_efi_pxe_config.template'),
               help=_('On ironic-conductor node, template file for PXE '
                      'configuration for UEFI boot loader.')),
    cfg.StrOpt('tftp_server',
               default='$my_ip',
               help=_("IP address of ironic-conductor node's TFTP server.")),
    cfg.StrOpt('tftp_root',
               default='/tftpboot',
               help=_("ironic-conductor node's TFTP root path. The "
                      "ironic-conductor must have read/write access to this "
                      "path.")),
    cfg.StrOpt('tftp_master_path',
               default='/tftpboot/master_images',
               help=_('On ironic-conductor node, directory where master TFTP '
                      'images are stored on disk. '
                      'Setting to <None> disables image caching.')),
    # NOTE(dekehn): Additional boot files options may be created in the event
    #  other architectures require different boot files.
    cfg.StrOpt('pxe_bootfile_name',
               default='pxelinux.0',
               help=_('Bootfile DHCP parameter.')),
    cfg.StrOpt('uefi_pxe_bootfile_name',
               default='elilo.efi',
               help=_('Bootfile DHCP parameter for UEFI boot mode.')),
    cfg.BoolOpt('ipxe_enabled',
                default=False,
                help=_('Enable iPXE boot.')),
    cfg.StrOpt('ipxe_boot_script',
               default=paths.basedir_def(
                   'drivers/modules/boot.ipxe'),
               help=_('On ironic-conductor node, the path to the main iPXE '
                      'script file.')),
    cfg.StrOpt('ip_version',
               default='4',
               choices=['4', '6'],
               help=_('The IP version that will be used for PXE booting. '
                      'Can be either 4 or 6. Defaults to 4. EXPERIMENTAL')),
]

LOG = logging.getLogger(__name__)

CONF = cfg.CONF
CONF.register_opts(pxe_opts, group='pxe')
CONF.import_opt('deploy_callback_timeout', 'ironic.conductor.manager',
                group='conductor')

REQUIRED_PROPERTIES = {
    'deploy_kernel': _("UUID (from Glance) of the deployment kernel. "
                       "Required."),
    'deploy_ramdisk': _("UUID (from Glance) of the ramdisk that is "
                        "mounted at boot time. Required."),
}
COMMON_PROPERTIES = REQUIRED_PROPERTIES


# TODO(rameshg87): This method is only for allowing time for deployers to
# migrate to CONF.pxe.<options> after the CONF.agent.<pxe-options> have been
# deprecated. Remove this in Mitaka release.
def _get_pxe_conf_option(task, opt_name):
    """Returns the value of PXEBoot provided CONF option.

    This method returns the value of PXEBoot CONF option after checking
    the driver.deploy. If driver.deploy is AgentDeploy and the value of
    the CONF option is not it's default value, it returns the value of
    CONF.agent.agent_<opt_name>.  Otherwise, it returns the value of
    CONF.pxe.<opt_name>.  There are only 2 such parameters right now -
    pxe_config_template and pxe_append_params.  Caller
    has to make sure that only these 2 options are passed.

    :param task: TaskManager instance.
    :param opt_name: The CONF opt whose value is desired.
    :returns: The value of the CONF option.
    :raises: AttributeError, if such a CONF option doesn't exist.
    """
    if isinstance(task.driver.deploy, agent.AgentDeploy):
        agent_opt_name = 'agent_' + opt_name
        current_value = getattr(CONF.agent, agent_opt_name)
        opt_object = [x for x in agent.agent_opts
                      if x.name == agent_opt_name][0]
        default_value = opt_object.default
        # Replace $pybasedir which can occur in pxe_config_template
        # default value.
        default_value = default_value.replace('$pybasedir',
                                              CONF.pybasedir)

        if current_value != default_value:
            LOG.warning(
                _LW("The CONF option [agent]agent_%(opt_name)s is "
                    "deprecated and will be removed in Mitaka release of "
                    "Ironic. Please use [pxe]%(opt_name)s instead."),
                {'opt_name': opt_name})
            return current_value

    # Either task.driver.deploy is ISCSIDeploy() or the default value hasn't
    # been modified. So return the value of corresponding parameter in
    # [pxe] group.
    return getattr(CONF.pxe, opt_name)


def _parse_driver_info(node):
    """Gets the driver specific Node deployment info.

    This method validates whether the 'driver_info' property of the
    supplied node contains the required information for this driver to
    deploy images to the node.

    :param node: a single Node.
    :returns: A dict with the driver_info values.
    :raises: MissingParameterValue
    """
    info = node.driver_info
    d_info = {k: info.get(k) for k in ('deploy_kernel', 'deploy_ramdisk')}
    error_msg = _("Cannot validate PXE bootloader. Some parameters were"
                  " missing in node's driver_info")
    deploy_utils.check_for_missing_params(d_info, error_msg)
    return d_info


def _get_instance_image_info(node, ctx):
    """Generate the paths for TFTP files for instance related images.

    This method generates the paths for instance kernel and
    instance ramdisk. This method also updates the node, so caller should
    already have a non-shared lock on the node.

    :param node: a node object
    :param ctx: context
    :returns: a dictionary whose keys are the names of the images (kernel,
        ramdisk) and values are the absolute paths of them. If it's a whole
        disk image, it returns an empty dictionary.
    """
    image_info = {}
    if node.driver_internal_info.get('is_whole_disk_image'):
        return image_info

    root_dir = pxe_utils.get_root_dir()
    i_info = node.instance_info
    labels = ('kernel', 'ramdisk')
    d_info = deploy_utils.get_image_instance_info(node)
    if not (i_info.get('kernel') and i_info.get('ramdisk')):
        glance_service = service.GlanceImageService(version=1, context=ctx)
        iproperties = glance_service.show(d_info['image_source'])['properties']
        for label in labels:
            i_info[label] = str(iproperties[label + '_id'])
        node.instance_info = i_info
        node.save()

    for label in labels:
        image_info[label] = (
            i_info[label],
            os.path.join(root_dir, node.uuid, label)
        )

    return image_info


def _get_deploy_image_info(node):
    """Generate the paths for TFTP files for deploy images.

    This method generates the paths for the deploy kernel and
    deploy ramdisk.

    :param node: a node object
    :returns: a dictionary whose keys are the names of the images (
        deploy_kernel, deploy_ramdisk) and values are the absolute
        paths of them.
    :raises: MissingParameterValue, if deploy_kernel/deploy_ramdisk is
        missing in node's driver_info.
    """
    d_info = _parse_driver_info(node)
    return pxe_utils.get_deploy_kr_info(node.uuid, d_info)


def _build_pxe_config_options(task, pxe_info):
    """Build the PXE config options for a node

    This method builds the PXE boot options for a node,
    given all the required parameters.

    The options should then be passed to pxe_utils.create_pxe_config to
    create the actual config files.

    :param task: A TaskManager object
    :param pxe_info: a dict of values to set on the configuration file
    :returns: A dictionary of pxe options to be used in the pxe bootfile
        template.
    """
    node = task.node
    is_whole_disk_image = node.driver_internal_info.get('is_whole_disk_image')

    # These are dummy values to satisfy elilo.
    # image and initrd fields in elilo config cannot be blank.
    kernel = 'no_kernel'
    ramdisk = 'no_ramdisk'

    if CONF.pxe.ipxe_enabled:
        deploy_kernel = '/'.join([CONF.deploy.http_url, node.uuid,
                                  'deploy_kernel'])
        deploy_ramdisk = '/'.join([CONF.deploy.http_url, node.uuid,
                                   'deploy_ramdisk'])
        if not is_whole_disk_image:
            kernel = '/'.join([CONF.deploy.http_url, node.uuid,
                              'kernel'])
            ramdisk = '/'.join([CONF.deploy.http_url, node.uuid,
                               'ramdisk'])
    else:
        deploy_kernel = pxe_info['deploy_kernel'][1]
        deploy_ramdisk = pxe_info['deploy_ramdisk'][1]
        if not is_whole_disk_image:
            # It is possible that we don't have kernel/ramdisk or even
            # image_source to determine if it's a whole disk image or not.
            # For example, when transitioning to 'available' state for first
            # time from 'manage' state. Retain dummy values if we don't have
            # kernel/ramdisk.
            if 'kernel' in pxe_info:
                kernel = pxe_info['kernel'][1]
            if 'ramdisk' in pxe_info:
                ramdisk = pxe_info['ramdisk'][1]

    pxe_options = {
        'deployment_aki_path': deploy_kernel,
        'deployment_ari_path': deploy_ramdisk,
        'pxe_append_params': _get_pxe_conf_option(task, 'pxe_append_params'),
        'tftp_server': CONF.pxe.tftp_server,
        'aki_path': kernel,
        'ari_path': ramdisk
    }

    return pxe_options


def validate_boot_option_for_uefi(node):
    """In uefi boot mode, validate if the boot option is compatible.

    This method raises exception if whole disk image being deployed
    in UEFI boot mode without 'boot_option' being set to 'local'.

    :param node: a single Node.
    :raises: InvalidParameterValue
    """
    boot_mode = deploy_utils.get_boot_mode_for_deploy(node)
    boot_option = deploy_utils.get_boot_option(node)
    if (boot_mode == 'uefi' and
            node.driver_internal_info.get('is_whole_disk_image') and
            boot_option != 'local'):
        LOG.error(_LE("Whole disk image with netboot is not supported in UEFI "
                      "boot mode."))
        raise exception.InvalidParameterValue(_(
            "Conflict: Whole disk image being used for deploy, but "
            "cannot be used with node %(node_uuid)s configured to use "
            "UEFI boot with netboot option") %
            {'node_uuid': node.uuid})


def validate_boot_parameters_for_trusted_boot(node):
    """Check if boot parameters are valid for trusted boot."""
    boot_mode = deploy_utils.get_boot_mode_for_deploy(node)
    boot_option = deploy_utils.get_boot_option(node)
    is_whole_disk_image = node.driver_internal_info.get('is_whole_disk_image')
    # 'is_whole_disk_image' is not supported by trusted boot, because there is
    # no Kernel/Ramdisk to measure at all.
    if (boot_mode != 'bios' or
        is_whole_disk_image or
        boot_option != 'netboot'):
        msg = (_("Trusted boot is only supported in BIOS boot mode with "
                 "netboot and without whole_disk_image, but Node "
                 "%(node_uuid)s was configured with boot_mode: %(boot_mode)s, "
                 "boot_option: %(boot_option)s, is_whole_disk_image: "
                 "%(is_whole_disk_image)s: at least one of them is wrong, and "
                 "this can be caused by enable secure boot.") %
               {'node_uuid': node.uuid, 'boot_mode': boot_mode,
                'boot_option': boot_option,
                'is_whole_disk_image': is_whole_disk_image})
        LOG.error(msg)
        raise exception.InvalidParameterValue(msg)


@image_cache.cleanup(priority=25)
class TFTPImageCache(image_cache.ImageCache):
    def __init__(self):
        super(TFTPImageCache, self).__init__(
            CONF.pxe.tftp_master_path,
            # MiB -> B
            cache_size=CONF.pxe.image_cache_size * 1024 * 1024,
            # min -> sec
            cache_ttl=CONF.pxe.image_cache_ttl * 60)


def _cache_ramdisk_kernel(ctx, node, pxe_info):
    """Fetch the necessary kernels and ramdisks for the instance."""
    fileutils.ensure_tree(
        os.path.join(pxe_utils.get_root_dir(), node.uuid))
    LOG.debug("Fetching necessary kernel and ramdisk for node %s",
              node.uuid)
    deploy_utils.fetch_images(ctx, TFTPImageCache(), list(pxe_info.values()),
                              CONF.force_raw_images)


def _clean_up_pxe_env(task, images_info):
    """Cleanup PXE environment of all the images in images_info.

    Cleans up the PXE environment for the mentioned images in
    images_info.

    :param task: a TaskManager object
    :param images_info: A dictionary of images whose keys are the image names
        to be cleaned up (kernel, ramdisk, etc) and values are a tuple of
        identifier and absolute path.
    """
    for label in images_info:
        path = images_info[label][1]
        utils.unlink_without_raise(path)

    pxe_utils.clean_up_pxe_config(task)
    TFTPImageCache().clean_up()


class PXEBoot(base.BootInterface):

    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        return COMMON_PROPERTIES

    def validate(self, task):
        """Validate the PXE-specific info for booting deploy/instance images.

        This method validates the PXE-specific info for booting the
        ramdisk and instance on the node.  If invalid, raises an
        exception; otherwise returns None.

        :param task: a task from TaskManager.
        :returns: None
        :raises: InvalidParameterValue, if some parameters are invalid.
        :raises: MissingParameterValue, if some required parameters are
            missing.
        """
        node = task.node

        if not driver_utils.get_node_mac_addresses(task):
            raise exception.MissingParameterValue(
                _("Node %s does not have any port associated with it.")
                % node.uuid)

        # Get the boot_mode capability value.
        boot_mode = deploy_utils.get_boot_mode_for_deploy(node)

        if CONF.pxe.ipxe_enabled:
            if (not CONF.deploy.http_url or
                not CONF.deploy.http_root):
                raise exception.MissingParameterValue(_(
                    "iPXE boot is enabled but no HTTP URL or HTTP "
                    "root was specified."))
            # iPXE and UEFI should not be configured together.
            if boot_mode == 'uefi':
                LOG.error(_LE("UEFI boot mode is not supported with "
                              "iPXE boot enabled."))
                raise exception.InvalidParameterValue(_(
                    "Conflict: iPXE is enabled, but cannot be used with node"
                    "%(node_uuid)s configured to use UEFI boot") %
                    {'node_uuid': node.uuid})

        if boot_mode == 'uefi':
            validate_boot_option_for_uefi(node)

        # Check the trusted_boot capabilities value.
        deploy_utils.validate_capabilities(node)
        if deploy_utils.is_trusted_boot_requested(node):
            # Check if 'boot_option' and boot mode is compatible with
            # trusted boot.
            validate_boot_parameters_for_trusted_boot(node)

        _parse_driver_info(node)
        d_info = deploy_utils.get_image_instance_info(node)
        if node.driver_internal_info.get('is_whole_disk_image'):
            props = []
        elif service_utils.is_glance_image(d_info['image_source']):
            props = ['kernel_id', 'ramdisk_id']
        else:
            props = ['kernel', 'ramdisk']
        deploy_utils.validate_image_properties(task.context, d_info, props)

    def prepare_ramdisk(self, task, ramdisk_params):
        """Prepares the boot of Ironic ramdisk using PXE.

        This method prepares the boot of the deploy kernel/ramdisk after
        reading relevant information from the node's driver_info and
        instance_info.

        :param task: a task from TaskManager.
        :param ramdisk_params: the parameters to be passed to the ramdisk.
            pxe driver passes these parameters as kernel command-line
            arguments.
        :returns: None
        :raises: MissingParameterValue, if some information is missing in
            node's driver_info or instance_info.
        :raises: InvalidParameterValue, if some information provided is
            invalid.
        :raises: IronicException, if some power or set boot boot device
            operation failed on the node.
        """
        node = task.node

        # TODO(deva): optimize this if rerun on existing files
        if CONF.pxe.ipxe_enabled:
            # Copy the iPXE boot script to HTTP root directory
            bootfile_path = os.path.join(
                CONF.deploy.http_root,
                os.path.basename(CONF.pxe.ipxe_boot_script))
            shutil.copyfile(CONF.pxe.ipxe_boot_script, bootfile_path)

        dhcp_opts = pxe_utils.dhcp_options_for_instance(task)
        provider = dhcp_factory.DHCPFactory()
        provider.update_dhcp(task, dhcp_opts)

        pxe_info = _get_deploy_image_info(node)

        # NODE: Try to validate and fetch instance images only
        # if we are in DEPLOYING state.
        if node.provision_state == states.DEPLOYING:
            pxe_info.update(_get_instance_image_info(node, task.context))

        pxe_options = _build_pxe_config_options(task, pxe_info)
        pxe_options.update(ramdisk_params)

        if deploy_utils.get_boot_mode_for_deploy(node) == 'uefi':
            pxe_config_template = CONF.pxe.uefi_pxe_config_template
        else:
            pxe_config_template = _get_pxe_conf_option(task,
                                                       'pxe_config_template')

        pxe_utils.create_pxe_config(task, pxe_options,
                                    pxe_config_template)
        deploy_utils.try_set_boot_device(task, boot_devices.PXE)

        # FIXME(lucasagomes): If it's local boot we should not cache
        # the image kernel and ramdisk (Or even require it).
        _cache_ramdisk_kernel(task.context, node, pxe_info)

    def clean_up_ramdisk(self, task):
        """Cleans up the boot of ironic ramdisk.

        This method cleans up the PXE environment that was setup for booting
        the deploy ramdisk. It unlinks the deploy kernel/ramdisk in the node's
        directory in tftproot and removes it's PXE config.

        :param task: a task from TaskManager.
        :returns: None
        """
        node = task.node
        try:
            images_info = _get_deploy_image_info(node)
        except exception.MissingParameterValue as e:
            LOG.warning(_LW('Could not get deploy image info '
                            'to clean up images for node %(node)s: %(err)s'),
                        {'node': node.uuid, 'err': e})
        else:
            _clean_up_pxe_env(task, images_info)

    def prepare_instance(self, task):
        """Prepares the boot of instance.

        This method prepares the boot of the instance after reading
        relevant information from the node's instance_info. In case of netboot,
        it updates the dhcp entries and switches the PXE config. In case of
        localboot, it cleans up the PXE config.

        :param task: a task from TaskManager.
        :returns: None
        """
        node = task.node
        boot_option = deploy_utils.get_boot_option(node)

        if boot_option != "local":
            # Make sure that the instance kernel/ramdisk is cached.
            # This is for the takeover scenario for active nodes.
            instance_image_info = _get_instance_image_info(
                task.node, task.context)
            _cache_ramdisk_kernel(task.context, task.node, instance_image_info)

            # If it's going to PXE boot we need to update the DHCP server
            dhcp_opts = pxe_utils.dhcp_options_for_instance(task)
            provider = dhcp_factory.DHCPFactory()
            provider.update_dhcp(task, dhcp_opts)

            iwdi = task.node.driver_internal_info.get('is_whole_disk_image')
            try:
                root_uuid_or_disk_id = task.node.driver_internal_info[
                    'root_uuid_or_disk_id'
                ]
            except KeyError:
                if not iwdi:
                    LOG.warning(
                        _LW("The UUID for the root partition can't be "
                            "found, unable to switch the pxe config from "
                            "deployment mode to service (boot) mode for "
                            "node %(node)s"), {"node": task.node.uuid})
                else:
                    LOG.warning(
                        _LW("The disk id for the whole disk image can't "
                            "be found, unable to switch the pxe config "
                            "from deployment mode to service (boot) mode "
                            "for node %(node)s"),
                        {"node": task.node.uuid})
            else:
                pxe_config_path = pxe_utils.get_pxe_config_file_path(
                    task.node.uuid)
                deploy_utils.switch_pxe_config(
                    pxe_config_path, root_uuid_or_disk_id,
                    deploy_utils.get_boot_mode_for_deploy(node),
                    iwdi, deploy_utils.is_trusted_boot_requested(node))
                # In case boot mode changes from bios to uefi, boot device
                # order may get lost in some platforms. Better to re-apply
                # boot device.
                deploy_utils.try_set_boot_device(task, boot_devices.PXE)
        else:
            # If it's going to boot from the local disk, we don't need
            # PXE config files. They still need to be generated as part
            # of the prepare() because the deployment does PXE boot the
            # deploy ramdisk
            pxe_utils.clean_up_pxe_config(task)
            deploy_utils.try_set_boot_device(task, boot_devices.DISK)

    def clean_up_instance(self, task):
        """Cleans up the boot of instance.

        This method cleans up the environment that was setup for booting
        the instance. It unlinks the instance kernel/ramdisk in node's
        directory in tftproot and removes the PXE config.

        :param task: a task from TaskManager.
        :returns: None
        """
        node = task.node
        try:
            images_info = _get_instance_image_info(node, task.context)
        except exception.MissingParameterValue as e:
            LOG.warning(_LW('Could not get instance image info '
                            'to clean up images for node %(node)s: %(err)s'),
                        {'node': node.uuid, 'err': e})
        else:
            _clean_up_pxe_env(task, images_info)

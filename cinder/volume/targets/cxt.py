# Copyright 2015 Chelsio Communications Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import os
import re

from oslo_concurrency import processutils as putils
from oslo_utils import netutils

from cinder import exception
from cinder.openstack.common import fileutils
from cinder.i18n import _LI, _LW, _LE
from cinder.openstack.common import log as logging
from cinder import utils
from cinder.volume.targets import iscsi

LOG = logging.getLogger(__name__)


class CxtAdm(iscsi.ISCSITarget):
    """Chiscsi target configuration for block storage devices.
    This includes things like create targets, attach, detach
    etc.
    """

    TARGET_FMT = """
               target:
                       TargetName=%s
                       TargetDevice=%s
                       PortalGroup=1@%s
                 """
    TARGET_FMT_WITH_CHAP = """
                         target:
                                 TargetName=%s
                                 TargetDevice=%s
                                 PortalGroup=1@%s
                                 AuthMethod=CHAP
                                 Auth_CHAP_Policy=Oneway
                                 Auth_CHAP_Initiator=%s
                           """

    cxt_subdir = 'cxt'

    def __init__(self, *args, **kwargs):
        super(CxtAdm, self).__init__(*args, **kwargs)
        self.volumes_dir = self.configuration.safe_get('volumes_dir')
        self.volumes_dir = os.path.join(self.volumes_dir, self.cxt_subdir)
        self.config = self.configuration.safe_get('chiscsi_conf')

    def _get_volumes_dir(self):
        return self.volumes_dir

    def _get_target(self, iqn):
        # We can use target=iqn here, but iscsictl has no --brief mode, and
        # this way we save on a lot of unnecessary parsing
        (out, err) = utils.execute('iscsictl',
                                   '-c',
                                   'target=ALL',
                                   run_as_root=True)
        lines = out.split('\n')
        for line in lines:
            if iqn in line:
                parsed = line.split()
                tid = parsed[2]
                return tid[3:].rstrip(',')

        return None

    def _get_iscsi_target(self, context, vol_id):
        return 0

    def _get_target_and_lun(self, context, volume):
        lun = 0  # For chiscsi dev starts at lun 0
        iscsi_target = 1
        return iscsi_target, lun

    def _ensure_iscsi_targets(self, context, host):
        """Ensure that target ids have been created in datastore."""
        # NOTE : This is probably not required for chiscsi
        # TODO(jdg): In the future move all of the dependent stuff into the
        # cooresponding target admin class
        host_iscsi_targets = self.db.iscsi_target_count_by_host(context,
                                                                host)
        if host_iscsi_targets >= self.configuration.iscsi_num_targets:
            return

        # NOTE Chiscsi target ids start at 1.
        target_end = self.configuration.iscsi_num_targets + 1
        for target_num in xrange(1, target_end):
            target = {'host': host, 'target_num': target_num}
            self.db.iscsi_target_create_safe(context, target)

    def _get_target_chap_auth(self, context, name):
        volumes_dir = self._get_volumes_dir()
        vol_id = name.split(':')[1]
        volume_path = os.path.join(volumes_dir, vol_id)

        try:
            with open(volume_path, 'r') as f:
                volume_conf = f.read()
        except IOError as e_fnf:
            LOG.debug('Failed to open config for %(vol_id)s: %(e)s',
                      {'vol_id': vol_id, 'e': e_fnf})
            # We don't run on anything non-linux
            if e_fnf.errno == 2:
                return None
            else:
                raise
        except Exception as e_vol:
            LOG.error(_LE('Failed to open config for %(vol_id)s: %(e)s'),
                      {'vol_id': vol_id, 'e': e_vol})
            raise

        m = re.search('Auth_CHAP_Initiator="(\w+)":"(\w+)"', volume_conf)
        if m:
            return (m.group(1), m.group(2))
        LOG.debug('Failed to find CHAP auth from config for %s', vol_id)
        return None

    def create_iscsi_target(self, name, tid, lun, path,
                            chap_auth=None, **kwargs):

        (out, err) = utils.execute('iscsictl',
                                   '-c',
                                   'target=ALL',
                                   run_as_root=True)
        LOG.debug("Targets prior to update: %s", out)
        volumes_dir = self._get_volumes_dir()
        fileutils.ensure_tree(volumes_dir)

        vol_id = name.split(':')[1]

        if netutils.is_valid_ipv4(self.configuration.iscsi_ip_address):
            portal = "%s:%s" % (self.configuration.iscsi_ip_address,
                                self.configuration.iscsi_port)
        else:
            # ipv6 addresses use [ip]:port format, ipv4 use ip:port
            portal = "[%s]:%s" % (self.configuration.iscsi_ip_address,
                                  self.configuration.iscsi_port)

        if chap_auth is None:
            volume_conf = self.TARGET_FMT % (name, path, portal)
        else:
            volume_conf = self.TARGET_FMT_WITH_CHAP % (name,
                                                       path, portal,
                                                       '"%s":"%s"' % chap_auth)
        LOG.debug('Creating iscsi_target for: %s', vol_id)
        volume_path = os.path.join(volumes_dir, vol_id)

        if os.path.exists(volume_path):
            LOG.warning(_LW('Persistence file already exists for volume, '
                            'found file at: %s'), volume_path)
        f = open(volume_path, 'w+')
        f.write(volume_conf)
        f.close()
        LOG.debug('Created volume path %(vp)s,\n'
                  'content: %(vc)s',
                  {'vp': volume_path, 'vc': volume_conf})

        old_persist_file = None
        old_name = kwargs.get('old_name', None)
        if old_name:
            LOG.debug('Detected old persistence file for volume '
                      '%{vol}s at %{old_name}s',
                      {'vol': vol_id, 'old_name': old_name})
            old_persist_file = os.path.join(volumes_dir, old_name)

        try:
            # With the persistent tgts we create them
            # by creating the entry in the persist file
            # and then doing an update to get the target
            # created.
            (out, err) = utils.execute('iscsictl', '-S', 'target=%s' % name,
                                       '-f', volume_path,
                                       '-x', self.config,
                                       run_as_root=True)
        except putils.ProcessExecutionError as e:
            LOG.error(_LE("Failed to create iscsi target for volume "
                          "id:%(vol_id)s: %(e)s"),
                      {'vol_id': vol_id, 'e': e})

            # Don't forget to remove the persistent file we created
            os.unlink(volume_path)
            raise exception.ISCSITargetCreateFailed(volume_id=vol_id)
        finally:
            LOG.debug("StdOut from iscsictl -S: %s", out)
            LOG.debug("StdErr from iscsictl -S: %s", err)

        # Grab targets list for debug
        (out, err) = utils.execute('iscsictl',
                                   '-c',
                                   'target=ALL',
                                   run_as_root=True)
        LOG.debug("Targets after update: %s", out)

        iqn = '%s%s' % (self.iscsi_target_prefix, vol_id)
        tid = self._get_target(iqn)
        if tid is None:
            LOG.error(_LE("Failed to create iscsi target for volume "
                          "id:%(vol_id)s. Please verify your configuration "
                          "in %(volumes_dir)'"), {
                      'vol_id': vol_id,
                      'volumes_dir': volumes_dir, })
            raise exception.NotFound()

        if old_persist_file is not None and os.path.exists(old_persist_file):
            os.unlink(old_persist_file)

        return tid

    def remove_iscsi_target(self, tid, lun, vol_id, vol_name, **kwargs):
        LOG.info(_LI('Removing iscsi_target for: %s'), vol_id)
        vol_uuid_file = vol_name
        volume_path = os.path.join(self._get_volumes_dir(), vol_uuid_file)
        if not os.path.exists(volume_path):
            LOG.warning(_LW('Volume path %s does not exist, '
                            'nothing to remove.'), volume_path)
            return

        if os.path.isfile(volume_path):
            iqn = '%s%s' % (self.iscsi_target_prefix,
                            vol_uuid_file)
        else:
            raise exception.ISCSITargetRemoveFailed(volume_id=vol_id)

        target_exists = False
        try:
            (out, err) = utils.execute('iscsictl',
                                       '-c',
                                       'target=%s' % iqn,
                                       run_as_root=True)
            LOG.debug("StdOut from iscsictl -c: %s", out)
            LOG.debug("StdErr from iscsictl -c: %s", err)
        except putils.ProcessExecutionError as e:
            if "NOT found" in e.stdout:
                LOG.info(_LI("No iscsi target present for volume "
                             "id:%(vol_id)s: %(e)s"),
                         {'vol_id': vol_id, 'e': e})
                return
            else:
                raise exception.ISCSITargetRemoveFailed(volume_id=vol_id)
        else:
            target_exists = True

        try:
            utils.execute('iscsictl',
                          '-s',
                          'target=%s' % iqn,
                          run_as_root=True)
        except putils.ProcessExecutionError as e:
            # There exists a race condition where multiple calls to
            # remove_iscsi_target come in simultaneously. If we can poll
            # for a target successfully but it is gone before we can remove
            # it, fail silently
            if "is not found" in e.stderr and target_exists:
                LOG.info(_LI("No iscsi target present for volume "
                             "id:%(vol_id)s: %(e)s"),
                         {'vol_id': vol_id, 'e': e})
                return
            else:
                LOG.error(_LE("Failed to remove iscsi target for volume "
                              "id:%(vol_id)s: %(e)s"),
                          {'vol_id': vol_id, 'e': e})
                raise exception.ISCSITargetRemoveFailed(volume_id=vol_id)

        # Carried over from tgt
        # NOTE(jdg): This *should* be there still but incase
        # it's not we don't care, so just ignore it if was
        # somehow deleted between entry of this method
        # and here
        if os.path.exists(volume_path):
            os.unlink(volume_path)
        else:
            LOG.debug('Volume path %s not found at end, '
                      'of remove_iscsi_target.', volume_path)

# Copyright (c) 2015 Mirantis, Inc.
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

"""Test class for Ironic libvirt power driver."""


import tempfile

import mock

from ironic.drivers.modules import lib_virt
from ironic.tests.unit.db import base as db_base
from ironic.tests.unit.db import utils as db_utils
from ironic.tests.unit.objects import utils as obj_utils


class LibvirtValidateParametersTestCase(db_base.DbTestCase):

    def test__parse_driver_info_good_password(self):
        # make sure we get back the expected things
        node = obj_utils.get_test_node(
            self.context,
            driver='fake_libvirt',
            driver_info=db_utils.get_test_libvirt_driver_info('ssh_key'))

        info = lib_virt._parse_driver_info(node)

        self.assertIsNotNone(info.get('host'))
        self.assertIsNotNone(info.get('libvirt_uri'))
        self.assertIsNotNone(info.get('ssh_key_filename'))
        self.assertIsNotNone(info.get('port'))
        self.assertIsNotNone(info.get('virt_type'))
        self.assertIsNotNone(info.get('cmd_set'))
        self.assertIsNotNone(info.get('uuid'))
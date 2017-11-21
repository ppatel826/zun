# Copyright 2013 UnitedStack Inc.
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

from oslo_log import log as logging
from oslo_utils import strutils
from oslo_utils import uuidutils
import pecan

from zun.api.controllers import base
from zun.api.controllers import link
from zun.api.controllers.v1 import collection
from zun.api.controllers.v1.schemas import containers as schema
from zun.api.controllers.v1.views import containers_view as view
from zun.api.controllers import versions
from zun.api import utils as api_utils
from zun.common import consts
from zun.common import exception
from zun.common.i18n import _
from zun.common import name_generator
from zun.common import policy
from zun.common import utils
from zun.common import validation
import zun.conf
from zun.network import model as network_model
from zun.network import neutron
from zun import objects
from zun.pci import request as pci_request
from zun.volume import cinder_api as cinder

CONF = zun.conf.CONF
LOG = logging.getLogger(__name__)
NETWORK_ATTACH_EXTERNAL = 'network:attach_external_network'


def check_policy_on_container(container, action):
    context = pecan.request.context
    policy.enforce(context, action, container, action=action)


class ContainerCollection(collection.Collection):
    """API representation of a collection of containers."""

    fields = {
        'containers',
        'next'
    }

    """A list containing containers objects"""

    def __init__(self, **kwargs):
        super(ContainerCollection, self).__init__(**kwargs)
        self._type = 'containers'

    @staticmethod
    def convert_with_links(rpc_containers, limit, url=None,
                           expand=False, **kwargs):
        collection = ContainerCollection()
        collection.containers = \
            [view.format_container(url, p) for p in rpc_containers]
        collection.next = collection.get_next(limit, url=url, **kwargs)
        return collection


class ContainersController(base.Controller):
    """Controller for Containers."""

    _custom_actions = {
        'start': ['POST'],
        'stop': ['POST'],
        'reboot': ['POST'],
        'pause': ['POST'],
        'unpause': ['POST'],
        'logs': ['GET'],
        'execute': ['POST'],
        'execute_resize': ['POST'],
        'kill': ['POST'],
        'rename': ['POST'],
        'attach': ['GET'],
        'resize': ['POST'],
        'top': ['GET'],
        'get_archive': ['GET'],
        'put_archive': ['POST'],
        'stats': ['GET'],
        'commit': ['POST'],
        'add_security_group': ['POST'],
        'network_detach': ['POST'],
        'network_attach': ['POST']
    }

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_all(self, **kwargs):
        """Retrieve a list of containers.

        """
        context = pecan.request.context
        policy.enforce(context, "container:get_all",
                       action="container:get_all")
        return self._get_containers_collection(**kwargs)

    def _get_containers_collection(self, **kwargs):
        context = pecan.request.context
        if utils.is_all_tenants(kwargs):
            policy.enforce(context, "container:get_all_all_tenants",
                           action="container:get_all_all_tenants")
            context.all_tenants = True
        compute_api = pecan.request.compute_api
        limit = api_utils.validate_limit(kwargs.get('limit'))
        sort_dir = api_utils.validate_sort_dir(kwargs.get('sort_dir', 'asc'))
        sort_key = kwargs.get('sort_key', 'id')
        resource_url = kwargs.get('resource_url')
        expand = kwargs.get('expand')

        filters = None
        marker_obj = None
        marker = kwargs.get('marker')
        if marker:
            marker_obj = objects.Container.get_by_uuid(context,
                                                       marker)
        containers = objects.Container.list(context,
                                            limit,
                                            marker_obj,
                                            sort_key,
                                            sort_dir,
                                            filters=filters)

        for i, c in enumerate(containers):
            try:
                containers[i] = compute_api.container_show(context, c)
            except Exception as e:
                LOG.exception("Error while list container %(uuid)s: "
                              "%(e)s.",
                              {'uuid': c.uuid, 'e': e})
                containers[i].status = consts.UNKNOWN

        return ContainerCollection.convert_with_links(containers, limit,
                                                      url=resource_url,
                                                      expand=expand,
                                                      sort_key=sort_key,
                                                      sort_dir=sort_dir)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_one(self, container_ident, **kwargs):
        """Retrieve information about the given container.

        :param container_ident: UUID or name of a container.
        """
        context = pecan.request.context
        if utils.is_all_tenants(kwargs):
            policy.enforce(context, "container:get_one_all_tenants",
                           action="container:get_one_all_tenants")
            context.all_tenants = True
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:get_one")
        compute_api = pecan.request.compute_api
        container = compute_api.container_show(context, container)
        return view.format_container(pecan.request.host_url, container)

    def _generate_name_for_container(self):
        """Generate a random name like: zeta-22-container."""
        name_gen = name_generator.NameGenerator()
        name = name_gen.generate()
        return name + '-container'

    @pecan.expose('json')
    @api_utils.enforce_content_types(['application/json'])
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_create)
    @validation.validated(schema.container_create)
    def post(self, run=False, **container_dict):
        """Create a new container.

        :param run: if true, starts the container
        :param container_dict: a container within the request body.
        """
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        policy.enforce(context, "container:create",
                       action="container:create")

        # remove duplicate security_groups from list
        if container_dict.get('security_groups'):
            container_dict['security_groups'] = list(
                set(container_dict.get('security_groups')))
        try:
            run = strutils.bool_from_string(run, strict=True)
            container_dict['interactive'] = strutils.bool_from_string(
                container_dict.get('interactive', False), strict=True)
        except ValueError:
            msg = _('Valid run or interactive value is ''true'', '
                    '"false", True, False, "True" and "False"')
            raise exception.InvalidValue(msg)

        auto_remove = container_dict.pop('auto_remove', None)
        if auto_remove is not None:
            req_version = pecan.request.version
            min_version = versions.Version('', '', '', '1.3')
            if req_version >= min_version:
                try:
                    container_dict['auto_remove'] = strutils.bool_from_string(
                        auto_remove, strict=True)
                except ValueError:
                    msg = _('Auto_remove value are true or false')
                    raise exception.InvalidValue(msg)
            else:
                raise exception.InvalidParamInVersion(param='auto_remove',
                                                      req_version=req_version,
                                                      min_version=min_version)

        runtime = container_dict.pop('runtime', None)
        if runtime is not None:
            req_version = pecan.request.version
            min_version = versions.Version('', '', '', '1.5')
            if req_version >= min_version:
                container_dict['runtime'] = runtime
            else:
                raise exception.InvalidParamInVersion(param='runtime',
                                                      req_version=req_version,
                                                      min_version=min_version)

        hostname = container_dict.pop('hostname', None)
        if hostname is not None:
            req_version = pecan.request.version
            min_version = versions.Version('', '', '', '1.9')
            if req_version >= min_version:
                container_dict['hostname'] = hostname
            else:
                raise exception.InvalidParamInVersion(param='hostname',
                                                      req_version=req_version,
                                                      min_version=min_version)

        nets = container_dict.get('nets', [])
        requested_networks = self._build_requested_networks(context, nets)
        pci_req = self._create_pci_requests_for_sriov_ports(context,
                                                            requested_networks)

        mounts = container_dict.pop('mounts', [])
        if mounts:
            req_version = pecan.request.version
            min_version = versions.Version('', '', '', '1.11')
            if req_version < min_version:
                raise exception.InvalidParamInVersion(param='mounts',
                                                      req_version=req_version,
                                                      min_version=min_version)

        requested_volumes = self._build_requested_volumes(context, mounts)

        # Valiadtion accepts 'None' so need to convert it to None
        if container_dict.get('image_driver'):
            container_dict['image_driver'] = api_utils.string_or_none(
                container_dict.get('image_driver'))

        # NOTE(mkrai): Intent here is to check the existence of image
        # before proceeding to create container. If image is not found,
        # container create will fail with 400 status.
        if CONF.api.enable_image_validation:
            images = compute_api.image_search(
                context, container_dict['image'],
                container_dict.get('image_driver'), True)
            if not images:
                raise exception.ImageNotFound(image=container_dict['image'])
        container_dict['project_id'] = context.project_id
        container_dict['user_id'] = context.user_id
        name = container_dict.get('name') or \
            self._generate_name_for_container()
        container_dict['name'] = name
        if container_dict.get('memory'):
            container_dict['memory'] = \
                str(container_dict['memory']) + 'M'
        if container_dict.get('restart_policy'):
            utils.check_for_restart_policy(container_dict)

        container_dict['status'] = consts.CREATING
        extra_spec = {}
        extra_spec['hints'] = container_dict.get('hints', None)
        extra_spec['pci_requests'] = pci_req
        new_container = objects.Container(context, **container_dict)
        new_container.create(context)

        kwargs = {}
        kwargs['extra_spec'] = extra_spec
        kwargs['requested_networks'] = requested_networks
        kwargs['requested_volumes'] = requested_volumes
        if pci_req.requests:
            kwargs['pci_requests'] = pci_req
        kwargs['run'] = run
        compute_api.container_create(context, new_container, **kwargs)
        # Set the HTTP Location Header
        pecan.response.location = link.build_url('containers',
                                                 new_container.uuid)
        pecan.response.status = 202
        return view.format_container(pecan.request.host_url, new_container)

    def _create_pci_requests_for_sriov_ports(self, context,
                                             requested_networks):
        pci_requests = objects.ContainerPCIRequests(requests=[])
        if not requested_networks:
            return pci_requests

        neutron_api = neutron.NeutronAPI(context)
        for request_net in requested_networks:
            phynet_name = None
            vnic_type = network_model.VNIC_TYPE_NORMAL

            if request_net.get('port'):
                vnic_type, phynet_name = self._get_port_vnic_info(
                    context, neutron_api, request_net['port'])
            pci_request_id = None
            if vnic_type in network_model.VNIC_TYPES_SRIOV:
                spec = {pci_request.PCI_NET_TAG: phynet_name}
                dev_type = pci_request.DEVICE_TYPE_FOR_VNIC_TYPE.get(vnic_type)
                if dev_type:
                    spec[pci_request.PCI_DEVICE_TYPE_TAG] = dev_type
                request = objects.ContainerPCIRequest(
                    count=1,
                    spec=[spec],
                    request_id=uuidutils.generate_uuid())
                pci_requests.requests.append(request)
                pci_request_id = request.request_id
            request_net['pci_request_id'] = pci_request_id
        return pci_requests

    def _get_port_vnic_info(self, context, neutron, port_id):
        """Retrieve port vnic info

        Invoked with a valid port_id.
        Return vnic type and the attached physical network name.
        """
        phynet_name = None
        port = self._show_port(context, port_id, neutron_client=neutron,
                               fields=['binding:vnic_type', 'network_id'])
        vnic_type = port.get('binding:vnic_type',
                             network_model.VNIC_TYPE_NORMAL)
        if vnic_type in network_model.VNIC_TYPES_SRIOV:
            net_id = port['network_id']
            phynet_name = self._get_phynet_info(context, net_id)
        return vnic_type, phynet_name

    def _show_port(self, context, port_id, neutron_client=None, fields=None):
        """Return the port for the client given the port id.

        :param context: Request context.
        :param port_id: The id of port to be queried.
        :param neutron_client: A neutron client.
        :param fields: The condition fields to query port data.
        :returns: A dict of port data.
                  e.g. {'port_id': 'abcd', 'fixed_ip_address': '1.2.3.4'}
        """
        if not neutron_client:
            neutron_client = neutron.NeutronAPI(context)
        if fields:
            result = neutron_client.show_port(port_id, fields=fields)
        else:
            result = neutron_client.show_port(port_id)
        return result.get('port')

    def _get_phynet_info(self, context, net_id):
        phynet_name = None
        # NOTE(hongbin): Use admin context here because non-admin users are
        # unable to retrieve provider:* attributes.
        admin_context = context.elevated()
        neutron_api = neutron.NeutronAPI(admin_context)
        network = neutron_api.show_network(
            net_id, fields='provider:physical_network')
        net = network.get('network')
        phynet_name = net.get('provider:physical_network')
        return phynet_name

    def _check_external_network_attach(self, context, nets):
        """Check if attaching to external network is permitted."""
        if not context.can(NETWORK_ATTACH_EXTERNAL,
                           fatal=False):
            for net in nets:
                if net.get('router:external') and not net.get('shared'):
                    raise exception.ExternalNetworkAttachForbidden(
                        network_uuid=net['network'])

    def _build_requested_networks(self, context, nets):
        neutron_api = neutron.NeutronAPI(context)
        requested_networks = []
        for net in nets:
            if net.get('port'):
                port = neutron_api.get_neutron_port(net['port'])
                neutron_api.ensure_neutron_port_usable(port)
                network = neutron_api.get_neutron_network(port['network_id'])
                requested_networks.append({'network': port['network_id'],
                                           'port': port['id'],
                                           'router:external':
                                               network.get('router:external'),
                                           'shared': network.get('shared'),
                                           'v4-fixed-ip': '',
                                           'v6-fixed-ip': '',
                                           'preserve_on_delete': True})
            elif net.get('network'):
                network = neutron_api.get_neutron_network(net['network'])
                requested_networks.append({'network': network['id'],
                                           'port': '',
                                           'router:external':
                                               network.get('router:external'),
                                           'shared': network.get('shared'),
                                           'v4-fixed-ip':
                                               net.get('v4-fixed-ip', ''),
                                           'v6-fixed-ip':
                                               net.get('v6-fixed-ip', ''),
                                           'preserve_on_delete': False})

        if not requested_networks:
            # Find an available neutron net and create docker network by
            # wrapping the neutron net.
            neutron_net = neutron_api.get_available_network()
            requested_networks.append({'network': neutron_net['id'],
                                       'port': '',
                                       'v4-fixed-ip': '',
                                       'v6-fixed-ip': '',
                                       'preserve_on_delete': False})

        self._check_external_network_attach(context, requested_networks)
        return requested_networks

    def _build_requested_volumes(self, context, mounts):
        # NOTE(hongbin): We assume cinder is the only volume provider here.
        # The logic needs to be re-visited if a second volume provider
        # (i.e. Manila) is introduced.
        cinder_api = cinder.CinderAPI(context)
        requested_volumes = []
        for mount in mounts:
            volume = cinder_api.search_volume(mount['source'])
            cinder_api.ensure_volume_usable(volume)
            volmapp = objects.VolumeMapping(
                context,
                volume_id=volume.id, volume_provider='cinder',
                container_path=mount['destination'],
                user_id=context.user_id,
                project_id=context.project_id)
            requested_volumes.append(volmapp)

        return requested_volumes

    def _check_security_group(self, context, security_group, container):
        if security_group.get("uuid"):
            security_group_id = security_group.get("uuid")
            if not uuidutils.is_uuid_like(security_group_id):
                raise exception.InvalidUUID(uuid=security_group_id)
            if security_group_id in container.security_groups:
                msg = _("security_group %s already present in container") % \
                    security_group_id
                raise exception.InvalidValue(msg)
        else:
            security_group_ids = utils.get_security_group_ids(
                context, [security_group['name']])
            if len(security_group_ids) > len(security_group):
                msg = _("Multiple security group matches "
                        "found for name %(name)s, use an ID "
                        "to be more specific. ") % security_group
                raise exception.Conflict(msg)
            else:
                security_group_id = security_group_ids[0]
        container_ports_detail = utils.list_ports(context, container)

        for container_port_detail in container_ports_detail:
            if security_group_id in container_port_detail['security_groups']:
                msg = _("security_group %s already present in container") % \
                    list(security_group.values())[0]
                raise exception.InvalidValue(msg)
        return security_group_id

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validated(schema.add_security_group)
    def add_security_group(self, container_ident, **security_group):
        """Add security group to an existing container.

        :param container_ident: UUID or Name of a container.
        :param security_group: security_group to be added to container.
        """

        container = utils.get_container(container_ident)
        check_policy_on_container(
            container.as_dict(), "container:add_security_group")
        utils.validate_container_state(container, 'add_security_group')

        # check if security group already presnt in container
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        security_group_id = self._check_security_group(
            context, security_group, container)
        compute_api.add_security_group(context, container,
                                       security_group_id)
        pecan.response.status = 202

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validated(schema.container_update)
    def patch(self, container_ident, **patch):
        """Update an existing container.

        :param container_ident: UUID or name of a container.
        :param patch: a json PATCH document to apply to this container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:update")
        utils.validate_container_state(container, 'update')
        if 'memory' in patch:
            patch['memory'] = str(patch['memory']) + 'M'
        if 'cpu' in patch:
            patch['cpu'] = float(patch['cpu'])
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        container = compute_api.container_update(context, container, patch)
        return view.format_container(pecan.request.host_url, container)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_rename)
    def rename(self, container_ident, name):
        """Rename an existing container.

        :param container_ident: UUID or Name of a container.
        :param patch: a json PATCH document to apply to this container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:rename")
        if container.name == name:
            raise exception.Conflict('The new name for the container is the '
                                     'same as the old name.')
        container.name = name
        context = pecan.request.context
        container.save(context)
        return view.format_container(pecan.request.host_url, container)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_delete)
    def delete(self, container_ident, force=False, **kwargs):
        """Delete a container.

        :param container_ident: UUID or Name of a container.
        :param force: If True, allow to force delete the container.
        """
        context = pecan.request.context
        if utils.is_all_tenants(kwargs):
            policy.enforce(context, "container:delete_all_tenants",
                           action="container:delete_all_tenants")
            context.all_tenants = True
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:delete")
        try:
            force = strutils.bool_from_string(force, strict=True)
        except ValueError:
            msg = _('Valid force values are true, false, 0, 1, yes and no')
            raise exception.InvalidValue(msg)
        stop = kwargs.pop('stop', False)
        try:
            stop = strutils.bool_from_string(stop, strict=True)
        except ValueError:
            msg = _('Valid stop values are true, false, 0, 1, yes and no')
            raise exception.InvalidValue(msg)
        compute_api = pecan.request.compute_api
        if not force and not stop:
            utils.validate_container_state(container, 'delete')
        elif force and not stop:
            req_version = pecan.request.version
            min_version = versions.Version('', '', '', '1.7')
            if req_version >= min_version:
                policy.enforce(context, "container:delete_force",
                               action="container:delete_force")
                utils.validate_container_state(container, 'delete_force')
# Remove this line temporarily for tempest issues.
#            else:
#                raise exception.InvalidParamInVersion(param='force',
#                                                      req_version=req_version,
#                                                      min_version=min_version)
        elif stop:
            req_version = pecan.request.version
            min_version = versions.Version('', '', '', '1.12')
            if req_version >= min_version:
                check_policy_on_container(container.as_dict(),
                                          "container:stop")
                utils.validate_container_state(container,
                                               'delete_after_stop')
                if container.status == consts.RUNNING:
                    LOG.debug('Calling compute.container_stop with %s '
                              'before delete',
                              container.uuid)
                    compute_api.container_stop(context, container, 10)
            else:
                raise exception.InvalidParamInVersion(param='stop',
                                                      req_version=req_version,
                                                      min_version=min_version)
        container.status = consts.DELETING
        compute_api.container_delete(context, container, force)
        pecan.response.status = 204

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def start(self, container_ident, **kwargs):
        """Start container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:start")
        utils.validate_container_state(container, 'start')
        LOG.debug('Calling compute.container_start with %s',
                  container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.container_start(context, container)
        pecan.response.status = 202

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_stop)
    def stop(self, container_ident, timeout=None, **kwargs):
        """Stop container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:stop")
        utils.validate_container_state(container, 'stop')
        LOG.debug('Calling compute.container_stop with %s',
                  container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.container_stop(context, container, timeout)
        pecan.response.status = 202

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_reboot)
    def reboot(self, container_ident, timeout=None, **kwargs):
        """Reboot container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:reboot")
        utils.validate_container_state(container, 'reboot')
        LOG.debug('Calling compute.container_reboot with %s',
                  container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.container_reboot(context, container, timeout)
        pecan.response.status = 202

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def pause(self, container_ident, **kwargs):
        """Pause container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:pause")
        utils.validate_container_state(container, 'pause')
        LOG.debug('Calling compute.container_pause with %s',
                  container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.container_pause(context, container)
        pecan.response.status = 202

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def unpause(self, container_ident, **kwargs):
        """Unpause container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:unpause")
        utils.validate_container_state(container, 'unpause')
        LOG.debug('Calling compute.container_unpause with %s',
                  container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.container_unpause(context, container)
        pecan.response.status = 202

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_logs)
    def logs(self, container_ident, stdout=True, stderr=True,
             timestamps=False, tail='all', since=None):
        """Get logs of the given container.

        :param container_ident: UUID or Name of a container.
        :param stdout: Get standard output if True.
        :param sterr: Get standard error if True.
        :param timestamps: Show timestamps.
        :param tail: Number of lines to show from the end of the logs.
                     (default: get all logs)
        :param since: Show logs since a given datetime or
                     integer epoch (in seconds).
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:logs")
        utils.validate_container_state(container, 'logs')
        try:
            stdout = strutils.bool_from_string(stdout, strict=True)
            stderr = strutils.bool_from_string(stderr, strict=True)
            timestamps = strutils.bool_from_string(timestamps, strict=True)
        except ValueError:
            msg = _('Valid stdout, stderr and timestamps values are ''true'', '
                    '"false", True, False, 0 and 1, yes and no')
            raise exception.InvalidValue(msg)
        LOG.debug('Calling compute.container_logs with %s', container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        return compute_api.container_logs(context, container, stdout, stderr,
                                          timestamps, tail, since)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request,
                                     schema.query_param_execute_command)
    def execute(self, container_ident, run=True, interactive=False, **kwargs):
        """Execute command in a running container.

        :param container_ident: UUID or Name of a container.
        :param run: If True, execute run.
        :param interactive: Keep STDIN open and allocate a
                            pseudo-TTY for interactive.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:execute")
        utils.validate_container_state(container, 'execute')
        try:
            run = strutils.bool_from_string(run, strict=True)
            interactive = strutils.bool_from_string(interactive, strict=True)
        except ValueError:
            msg = _('Valid run values are true, false, 0, 1, yes and no')
            raise exception.InvalidValue(msg)
        LOG.debug('Calling compute.container_exec with %(uuid)s command '
                  '%(command)s',
                  {'uuid': container.uuid, 'command': kwargs['command']})
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        return compute_api.container_exec(context, container,
                                          kwargs['command'],
                                          run, interactive)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request,
                                     schema.query_param_execute_resize)
    def execute_resize(self, container_ident, exec_id, **kwargs):
        """Resize the tty session used by the exec

        :param container_ident: UUID or Name of a container.
        :param exec_id: ID of a exec.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(),
                                  "container:execute_resize")
        utils.validate_container_state(container, 'execute_resize')
        LOG.debug('Calling tty resize used by exec %s', exec_id)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        return compute_api.container_exec_resize(
            context, container, exec_id, kwargs.get('h', None),
            kwargs.get('w', None))

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validated(schema.query_param_signal)
    def kill(self, container_ident, **kwargs):
        """Kill a running container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:kill")
        utils.validate_container_state(container, 'kill')
        LOG.debug('Calling compute.container_kill with %(uuid)s '
                  'signal %(signal)s',
                  {'uuid': container.uuid,
                   'signal': kwargs.get('signal')})
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.container_kill(context, container, kwargs.get('signal'))
        pecan.response.status = 202

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def attach(self, container_ident):
        """Attach to a running container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:attach")
        utils.validate_container_state(container, 'attach')
        LOG.debug('Checking the status for attach with %s', container.uuid)
        if container.interactive:
            context = pecan.request.context
            compute_api = pecan.request.compute_api
            url = compute_api.container_attach(context, container)
            return url
        msg = _("Container doesn't support to be attached, "
                "please check the interactive set properly")
        raise exception.NoInteractiveFlag(msg=msg)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_resize)
    def resize(self, container_ident, **kwargs):
        """Resize container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:resize")
        utils.validate_container_state(container, 'resize')
        LOG.debug('Calling tty resize with %s ', container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.container_resize(context, container, kwargs.get('h', None),
                                     kwargs.get('w', None))

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_top)
    def top(self, container_ident, ps_args=None):
        """Display the running processes inside the container.

        :param container_ident: UUID or Name of a container.
        :param ps_args: The args of the ps command.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:top")
        utils.validate_container_state(container, 'top')
        LOG.debug('Calling compute.container_top with %s', container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        return compute_api.container_top(context, container, ps_args)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_archive(self, container_ident, **kwargs):
        """Retrieve a file/folder from a container

        Retrieve a file or folder from a container in the
        form of a tar archive.
        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:get_archive")
        utils.validate_container_state(container, 'get_archive')
        LOG.debug('Calling compute.container_get_archive with %(uuid)s '
                  'path %(path)s',
                  {'uuid': container.uuid, 'path': kwargs['path']})
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        data, stat = compute_api.container_get_archive(
            context, container, kwargs['path'])
        return {"data": data, "stat": stat}

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def put_archive(self, container_ident, **kwargs):
        """Insert a file/folder to container.

        Insert a file or folder to an existing container using
        a tar archive as source.
        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:put_archive")
        utils.validate_container_state(container, 'put_archive')
        LOG.debug('Calling compute.container_put_archive with %(uuid)s '
                  'path %(path)s',
                  {'uuid': container.uuid, 'path': kwargs['path']})
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        compute_api.container_put_archive(context, container,
                                          kwargs['path'], kwargs['data'])

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def stats(self, container_ident):
        """Display stats snapshot of the container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:stats")
        utils.validate_container_state(container, 'stats')
        LOG.debug('Calling compute.container_stats with %s', container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        return compute_api.container_stats(context, container)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.query_param_commit)
    def commit(self, container_ident, **kwargs):
        """Create a new image from a container's changes.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(), "container:commit")
        utils.validate_container_state(container, 'commit')
        LOG.debug('Calling compute.container_commit %s ', container.uuid)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        pecan.response.status = 202
        return compute_api.container_commit(context, container,
                                            kwargs.get('repository', None),
                                            kwargs.get('tag', None))

    @base.Controller.api_version("1.6")
    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.network_detach)
    def network_detach(self, container_ident, **kwargs):
        """Detach a network from the container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(),
                                  "container:network_detach")
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        neutron_api = neutron.NeutronAPI(context)
        neutron_net = neutron_api.get_neutron_network(kwargs.get('network'))
        compute_api.network_detach(context, container, neutron_net['id'])
        pecan.response.status = 202

    @base.Controller.api_version("1.8")
    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    @validation.validate_query_param(pecan.request, schema.network_attach)
    def network_attach(self, container_ident, **kwargs):
        """Attach a network to the container.

        :param container_ident: UUID or Name of a container.
        """
        container = utils.get_container(container_ident)
        check_policy_on_container(container.as_dict(),
                                  "container:network_attach")
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        neutron_api = neutron.NeutronAPI(context)
        neutron_net = neutron_api.get_neutron_network(kwargs.get('network'))
        compute_api.network_attach(context, container, neutron_net['id'])

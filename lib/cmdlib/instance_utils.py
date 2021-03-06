#
#

# Copyright (C) 2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014 Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS
# IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.


"""Utility function mainly, but not only used by instance LU's."""

import logging
import os

from ganeti import constants
from ganeti import errors
from ganeti import locking
from ganeti import network
from ganeti import objects
from ganeti import pathutils
from ganeti import utils
from ganeti.cmdlib.common import AnnotateDiskParams, \
  ComputeIPolicyInstanceViolation, CheckDiskTemplateEnabled


def BuildInstanceHookEnv(name, primary_node_name, secondary_node_names, os_type,
                         status, minmem, maxmem, vcpus, nics, disk_template,
                         disks, bep, hvp, hypervisor_name, tags):
  """Builds instance related env variables for hooks

  This builds the hook environment from individual variables.

  @type name: string
  @param name: the name of the instance
  @type primary_node_name: string
  @param primary_node_name: the name of the instance's primary node
  @type secondary_node_names: list
  @param secondary_node_names: list of secondary nodes as strings
  @type os_type: string
  @param os_type: the name of the instance's OS
  @type status: string
  @param status: the desired status of the instance
  @type minmem: string
  @param minmem: the minimum memory size of the instance
  @type maxmem: string
  @param maxmem: the maximum memory size of the instance
  @type vcpus: string
  @param vcpus: the count of VCPUs the instance has
  @type nics: list
  @param nics: list of tuples (name, uuid, ip, mac, mode, link, vlan, net,
      netinfo) representing the NICs the instance has
  @type disk_template: string
  @param disk_template: the disk template of the instance
  @type disks: list
  @param disks: list of disks (either objects.Disk or dict)
  @type bep: dict
  @param bep: the backend parameters for the instance
  @type hvp: dict
  @param hvp: the hypervisor parameters for the instance
  @type hypervisor_name: string
  @param hypervisor_name: the hypervisor for the instance
  @type tags: list
  @param tags: list of instance tags as strings
  @rtype: dict
  @return: the hook environment for this instance

  """
  env = {
    "OP_TARGET": name,
    "INSTANCE_NAME": name,
    "INSTANCE_PRIMARY": primary_node_name,
    "INSTANCE_SECONDARIES": " ".join(secondary_node_names),
    "INSTANCE_OS_TYPE": os_type,
    "INSTANCE_STATUS": status,
    "INSTANCE_MINMEM": minmem,
    "INSTANCE_MAXMEM": maxmem,
    # TODO(2.9) remove deprecated "memory" value
    "INSTANCE_MEMORY": maxmem,
    "INSTANCE_VCPUS": vcpus,
    "INSTANCE_DISK_TEMPLATE": disk_template,
    "INSTANCE_HYPERVISOR": hypervisor_name,
    }
  if nics:
    nic_count = len(nics)
    for idx, (name, uuid, ip, mac, mode, link, vlan, net, netinfo) \
        in enumerate(nics):
      if ip is None:
        ip = ""
      if name:
        env["INSTANCE_NIC%d_NAME" % idx] = name
      env["INSTANCE_NIC%d_UUID" % idx] = uuid
      env["INSTANCE_NIC%d_IP" % idx] = ip
      env["INSTANCE_NIC%d_MAC" % idx] = mac
      env["INSTANCE_NIC%d_MODE" % idx] = mode
      env["INSTANCE_NIC%d_LINK" % idx] = link
      env["INSTANCE_NIC%d_VLAN" % idx] = vlan
      if netinfo:
        nobj = objects.Network.FromDict(netinfo)
        env.update(nobj.HooksDict("INSTANCE_NIC%d_" % idx))
      elif network:
        # FIXME: broken network reference: the instance NIC specifies a
        # network, but the relevant network entry was not in the config. This
        # should be made impossible.
        env["INSTANCE_NIC%d_NETWORK_NAME" % idx] = net
      if mode == constants.NIC_MODE_BRIDGED or \
         mode == constants.NIC_MODE_OVS:
        env["INSTANCE_NIC%d_BRIDGE" % idx] = link
  else:
    nic_count = 0

  env["INSTANCE_NIC_COUNT"] = nic_count

  if disks:
    disk_count = len(disks)
    for idx, disk in enumerate(disks):
      env.update(BuildDiskEnv(idx, disk))
  else:
    disk_count = 0

  env["INSTANCE_DISK_COUNT"] = disk_count

  if not tags:
    tags = []

  env["INSTANCE_TAGS"] = " ".join(tags)

  for source, kind in [(bep, "BE"), (hvp, "HV")]:
    for key, value in source.items():
      env["INSTANCE_%s_%s" % (kind, key)] = value

  return env


def BuildInstanceHookEnvByObject(lu, instance, secondary_nodes=None,
                                 disks=None, override=None):
  """Builds instance related env variables for hooks from an object.

  @type lu: L{LogicalUnit}
  @param lu: the logical unit on whose behalf we execute
  @type instance: L{objects.Instance}
  @param instance: the instance for which we should build the
      environment
  @type override: dict
  @param override: dictionary with key/values that will override
      our values
  @rtype: dict
  @return: the hook environment dictionary

  """
  cluster = lu.cfg.GetClusterInfo()
  bep = cluster.FillBE(instance)
  hvp = cluster.FillHV(instance)

  # Override secondary_nodes
  if secondary_nodes is None:
    secondary_nodes = lu.cfg.GetInstanceSecondaryNodes(instance.uuid)

  # Override disks
  if disks is None:
    disks = lu.cfg.GetInstanceDisks(instance.uuid)

  args = {
    "name": instance.name,
    "primary_node_name": lu.cfg.GetNodeName(instance.primary_node),
    "secondary_node_names": lu.cfg.GetNodeNames(secondary_nodes),
    "os_type": instance.os,
    "status": instance.admin_state,
    "maxmem": bep[constants.BE_MAXMEM],
    "minmem": bep[constants.BE_MINMEM],
    "vcpus": bep[constants.BE_VCPUS],
    "nics": NICListToTuple(lu, instance.nics),
    "disk_template": instance.disk_template,
    "disks": disks,
    "bep": bep,
    "hvp": hvp,
    "hypervisor_name": instance.hypervisor,
    "tags": instance.tags,
  }
  if override:
    args.update(override)
  return BuildInstanceHookEnv(**args) # pylint: disable=W0142


def GetClusterDomainSecret():
  """Reads the cluster domain secret.

  """
  return utils.ReadOneLineFile(pathutils.CLUSTER_DOMAIN_SECRET_FILE,
                               strict=True)


def CheckNodeNotDrained(lu, node_uuid):
  """Ensure that a given node is not drained.

  @param lu: the LU on behalf of which we make the check
  @param node_uuid: the node to check
  @raise errors.OpPrereqError: if the node is drained

  """
  node = lu.cfg.GetNodeInfo(node_uuid)
  if node.drained:
    raise errors.OpPrereqError("Can't use drained node %s" % node.name,
                               errors.ECODE_STATE)


def CheckNodeVmCapable(lu, node_uuid):
  """Ensure that a given node is vm capable.

  @param lu: the LU on behalf of which we make the check
  @param node_uuid: the node to check
  @raise errors.OpPrereqError: if the node is not vm capable

  """
  if not lu.cfg.GetNodeInfo(node_uuid).vm_capable:
    raise errors.OpPrereqError("Can't use non-vm_capable node %s" % node_uuid,
                               errors.ECODE_STATE)


def RemoveInstance(lu, feedback_fn, instance, ignore_failures):
  """Utility function to remove an instance.

  """
  logging.info("Removing block devices for instance %s", instance.name)

  if not RemoveDisks(lu, instance, ignore_failures=ignore_failures):
    if not ignore_failures:
      raise errors.OpExecError("Can't remove instance's disks")
    feedback_fn("Warning: can't remove instance's disks")

  logging.info("Removing instance's disks")
  for disk in instance.disks:
    lu.cfg.RemoveInstanceDisk(instance.uuid, disk)

  logging.info("Removing instance %s out of cluster config", instance.name)
  lu.cfg.RemoveInstance(instance.uuid)


def RemoveDisks(lu, instance, disk_template=None, disks=None,
                target_node_uuid=None, ignore_failures=False):
  """Remove all or a subset of disks for an instance.

  This abstracts away some work from `AddInstance()` and
  `RemoveInstance()`. Note that in case some of the devices couldn't
  be removed, the removal will continue with the other ones.

  This function is also used by the disk template conversion mechanism to
  remove the old block devices of the instance. Since the instance has
  changed its template at the time we remove the original disks, we must
  specify the template of the disks we are about to remove as an argument.

  @type lu: L{LogicalUnit}
  @param lu: the logical unit on whose behalf we execute
  @type instance: L{objects.Instance}
  @param instance: the instance whose disks we should remove
  @type disk_template: string
  @param disk_template: if passed, overrides the instance's disk_template
  @type disks: list of L{objects.Disk}
  @param disks: the disks to remove; if not specified, all the disks of the
          instance are removed
  @type target_node_uuid: string
  @param target_node_uuid: used to override the node on which to remove the
          disks
  @rtype: boolean
  @return: the success of the removal

  """
  logging.info("Removing block devices for instance %s", instance.name)

  all_result = True
  ports_to_release = set()

  disk_count = len(instance.disks)
  if disks is None:
    disks = lu.cfg.GetInstanceDisks(instance.uuid)

  if disk_template is None:
    disk_template = instance.disk_template

  anno_disks = AnnotateDiskParams(instance, disks, lu.cfg)
  for (idx, device) in enumerate(anno_disks):
    if target_node_uuid:
      edata = [(target_node_uuid, device)]
    else:
      edata = device.ComputeNodeTree(instance.primary_node)
    for node_uuid, disk in edata:
      result = lu.rpc.call_blockdev_remove(node_uuid, (disk, instance))
      if result.fail_msg:
        lu.LogWarning("Could not remove disk %s on node %s,"
                      " continuing anyway: %s", idx,
                      lu.cfg.GetNodeName(node_uuid), result.fail_msg)
        if not (result.offline and node_uuid != instance.primary_node):
          all_result = False

    # if this is a DRBD disk, return its port to the pool
    if device.dev_type in constants.DTS_DRBD:
      ports_to_release.add(device.logical_id[2])

  if all_result or ignore_failures:
    for port in ports_to_release:
      lu.cfg.AddTcpUdpPort(port)

  CheckDiskTemplateEnabled(lu.cfg.GetClusterInfo(), disk_template)

  if (len(disks) == disk_count and
      disk_template in [constants.DT_FILE, constants.DT_SHARED_FILE]):
    if len(disks) > 0:
      file_storage_dir = os.path.dirname(disks[0].logical_id[1])
    else:
      if disk_template == constants.DT_SHARED_FILE:
        file_storage_dir = utils.PathJoin(lu.cfg.GetSharedFileStorageDir(),
                                          instance.name)
      else:
        file_storage_dir = utils.PathJoin(lu.cfg.GetFileStorageDir(),
                                          instance.name)
    if target_node_uuid:
      tgt = target_node_uuid
    else:
      tgt = instance.primary_node
    result = lu.rpc.call_file_storage_dir_remove(tgt, file_storage_dir)
    if result.fail_msg:
      lu.LogWarning("Could not remove directory '%s' on node %s: %s",
                    file_storage_dir, lu.cfg.GetNodeName(tgt), result.fail_msg)
      all_result = False

  return all_result


def NICToTuple(lu, nic):
  """Build a tupple of nic information.

  @type lu:  L{LogicalUnit}
  @param lu: the logical unit on whose behalf we execute
  @type nic: L{objects.NIC}
  @param nic: nic to convert to hooks tuple

  """
  cluster = lu.cfg.GetClusterInfo()
  filled_params = cluster.SimpleFillNIC(nic.nicparams)
  mode = filled_params[constants.NIC_MODE]
  link = filled_params[constants.NIC_LINK]
  vlan = filled_params[constants.NIC_VLAN]
  netinfo = None
  if nic.network:
    nobj = lu.cfg.GetNetwork(nic.network)
    netinfo = objects.Network.ToDict(nobj)
  return (nic.name, nic.uuid, nic.ip, nic.mac, mode, link, vlan,
          nic.network, netinfo)


def NICListToTuple(lu, nics):
  """Build a list of nic information tuples.

  This list is suitable to be passed to _BuildInstanceHookEnv or as a return
  value in LUInstanceQueryData.

  @type lu:  L{LogicalUnit}
  @param lu: the logical unit on whose behalf we execute
  @type nics: list of L{objects.NIC}
  @param nics: list of nics to convert to hooks tuples

  """
  hooks_nics = []
  for nic in nics:
    hooks_nics.append(NICToTuple(lu, nic))
  return hooks_nics


def CopyLockList(names):
  """Makes a copy of a list of lock names.

  Handles L{locking.ALL_SET} correctly.

  """
  if names == locking.ALL_SET:
    return locking.ALL_SET
  else:
    return names[:]


def ReleaseLocks(lu, level, names=None, keep=None):
  """Releases locks owned by an LU.

  @type lu: L{LogicalUnit}
  @param level: Lock level
  @type names: list or None
  @param names: Names of locks to release
  @type keep: list or None
  @param keep: Names of locks to retain

  """
  logging.debug("Lu %s ReleaseLocks %s names=%s, keep=%s",
                lu.wconfdcontext, level, names, keep)
  assert not (keep is not None and names is not None), \
         "Only one of the 'names' and the 'keep' parameters can be given"

  if names is not None:
    should_release = names.__contains__
  elif keep:
    should_release = lambda name: name not in keep
  else:
    should_release = None

  levelname = locking.LEVEL_NAMES[level]

  owned = lu.owned_locks(level)
  if not owned:
    # Not owning any lock at this level, do nothing
    pass

  elif should_release:
    retain = []
    release = []

    # Determine which locks to release
    for name in owned:
      if should_release(name):
        release.append(name)
      else:
        retain.append(name)

    assert len(lu.owned_locks(level)) == (len(retain) + len(release))

    # Release just some locks
    lu.WConfdClient().TryUpdateLocks(
      lu.release_request(level, release))
    assert frozenset(lu.owned_locks(level)) == frozenset(retain)
  else:
    lu.WConfdClient().FreeLocksLevel(levelname)


def _ComputeIPolicyNodeViolation(ipolicy, instance, current_group,
                                 target_group, cfg,
                                 _compute_fn=ComputeIPolicyInstanceViolation):
  """Compute if instance meets the specs of the new target group.

  @param ipolicy: The ipolicy to verify
  @param instance: The instance object to verify
  @param current_group: The current group of the instance
  @param target_group: The new group of the instance
  @type cfg: L{config.ConfigWriter}
  @param cfg: Cluster configuration
  @param _compute_fn: The function to verify ipolicy (unittest only)
  @see: L{ganeti.cmdlib.common.ComputeIPolicySpecViolation}

  """
  if current_group == target_group:
    return []
  else:
    return _compute_fn(ipolicy, instance, cfg)


def CheckTargetNodeIPolicy(lu, ipolicy, instance, node, cfg, ignore=False,
                           _compute_fn=_ComputeIPolicyNodeViolation):
  """Checks that the target node is correct in terms of instance policy.

  @param ipolicy: The ipolicy to verify
  @param instance: The instance object to verify
  @param node: The new node to relocate
  @type cfg: L{config.ConfigWriter}
  @param cfg: Cluster configuration
  @param ignore: Ignore violations of the ipolicy
  @param _compute_fn: The function to verify ipolicy (unittest only)
  @see: L{ganeti.cmdlib.common.ComputeIPolicySpecViolation}

  """
  primary_node = lu.cfg.GetNodeInfo(instance.primary_node)
  res = _compute_fn(ipolicy, instance, primary_node.group, node.group, cfg)

  if res:
    msg = ("Instance does not meet target node group's (%s) instance"
           " policy: %s") % (node.group, utils.CommaJoin(res))
    if ignore:
      lu.LogWarning(msg)
    else:
      raise errors.OpPrereqError(msg, errors.ECODE_INVAL)


def GetInstanceInfoText(instance):
  """Compute that text that should be added to the disk's metadata.

  """
  return "originstname+%s" % instance.name


def CheckNodeFreeMemory(lu, node_uuid, reason, requested, hvname, hvparams):
  """Checks if a node has enough free memory.

  This function checks if a given node has the needed amount of free
  memory. In case the node has less memory or we cannot get the
  information from the node, this function raises an OpPrereqError
  exception.

  @type lu: C{LogicalUnit}
  @param lu: a logical unit from which we get configuration data
  @type node_uuid: C{str}
  @param node_uuid: the node to check
  @type reason: C{str}
  @param reason: string to use in the error message
  @type requested: C{int}
  @param requested: the amount of memory in MiB to check for
  @type hvname: string
  @param hvname: the hypervisor's name
  @type hvparams: dict of strings
  @param hvparams: the hypervisor's parameters
  @rtype: integer
  @return: node current free memory
  @raise errors.OpPrereqError: if the node doesn't have enough memory, or
      we cannot check the node

  """
  node_name = lu.cfg.GetNodeName(node_uuid)
  nodeinfo = lu.rpc.call_node_info([node_uuid], None, [(hvname, hvparams)])
  nodeinfo[node_uuid].Raise("Can't get data from node %s" % node_name,
                            prereq=True, ecode=errors.ECODE_ENVIRON)
  (_, _, (hv_info, )) = nodeinfo[node_uuid].payload

  free_mem = hv_info.get("memory_free", None)
  if not isinstance(free_mem, int):
    raise errors.OpPrereqError("Can't compute free memory on node %s, result"
                               " was '%s'" % (node_name, free_mem),
                               errors.ECODE_ENVIRON)
  if requested > free_mem:
    raise errors.OpPrereqError("Not enough memory on node %s for %s:"
                               " needed %s MiB, available %s MiB" %
                               (node_name, reason, requested, free_mem),
                               errors.ECODE_NORES)
  return free_mem


def CheckInstanceBridgesExist(lu, instance, node_uuid=None):
  """Check that the brigdes needed by an instance exist.

  """
  if node_uuid is None:
    node_uuid = instance.primary_node
  CheckNicsBridgesExist(lu, instance.nics, node_uuid)


def CheckNicsBridgesExist(lu, nics, node_uuid):
  """Check that the brigdes needed by a list of nics exist.

  """
  cluster = lu.cfg.GetClusterInfo()
  paramslist = [cluster.SimpleFillNIC(nic.nicparams) for nic in nics]
  brlist = [params[constants.NIC_LINK] for params in paramslist
            if params[constants.NIC_MODE] == constants.NIC_MODE_BRIDGED]
  if brlist:
    result = lu.rpc.call_bridges_exist(node_uuid, brlist)
    result.Raise("Error checking bridges on destination node '%s'" %
                 lu.cfg.GetNodeName(node_uuid), prereq=True,
                 ecode=errors.ECODE_ENVIRON)


def UpdateMetadata(feedback_fn, rpc, instance,
                   osparams_public=None,
                   osparams_private=None,
                   osparams_secret=None):
  """Updates instance metadata on the metadata daemon on the
  instance's primary node.

  If the daemon isn't available (not compiled), do nothing.

  In case the RPC fails, this function simply issues a warning and
  proceeds normally.

  @type feedback_fn: callable
  @param feedback_fn: function used send feedback back to the caller

  @type rpc: L{rpc.node.RpcRunner}
  @param rpc: RPC runner

  @type instance: L{objects.Instance}
  @param instance: instance for which the metadata should be updated

  @type osparams_public: NoneType or dict
  @param osparams_public: public OS parameters used to override those
                          defined in L{instance}

  @type osparams_private: NoneType or dict
  @param osparams_private: private OS parameters used to override those
                           defined in L{instance}

  @type osparams_secret: NoneType or dict
  @param osparams_secret: secret OS parameters used to override those
                          defined in L{instance}

  @rtype: NoneType
  @return: None

  """
  if not constants.ENABLE_METAD:
    return

  data = instance.ToDict()

  if osparams_public is not None:
    data["osparams_public"] = osparams_public

  if osparams_private is not None:
    data["osparams_private"] = osparams_private

  if osparams_secret is not None:
    data["osparams_secret"] = osparams_secret
  else:
    data["osparams_secret"] = {}

  result = rpc.call_instance_metadata_modify(instance.primary_node, data)
  result.Warn("Could not update metadata for instance '%s'" % instance.name,
              feedback_fn)


def CheckCompressionTool(lu, compression_tool):
  """ Checks if the provided compression tool is allowed to be used.

  @type compression_tool: string
  @param compression_tool: Compression tool to use for importing or exporting
    the instance

  @rtype: NoneType
  @return: None

  @raise errors.OpPrereqError: If the tool is not enabled by Ganeti or
                               whitelisted

  """
  allowed_tools = lu.cfg.GetCompressionTools()
  if (compression_tool != constants.IEC_NONE and
      compression_tool not in allowed_tools):
    raise errors.OpPrereqError(
      "Compression tool not allowed, tools allowed are [%s]"
      % ", ".join(allowed_tools), errors.ECODE_INVAL
    )


def BuildDiskLogicalIDEnv(idx, disk):
  """Helper method to create hooks env related to disk's logical_id

  @type idx: integer
  @param idx: The index of the disk
  @type disk: L{objects.Disk}
  @param disk: The disk object

  """
  if disk.dev_type == constants.DT_PLAIN:
    vg, name = disk.logical_id
    ret = {
      "INSTANCE_DISK%d_VG" % idx: vg,
      "INSTANCE_DISK%d_ID" % idx: name
      }
  elif disk.dev_type in (constants.DT_FILE, constants.DT_SHARED_FILE):
    file_driver, name = disk.logical_id
    ret = {
      "INSTANCE_DISK%d_DRIVER" % idx: file_driver,
      "INSTANCE_DISK%d_ID" % idx: name
      }
  elif disk.dev_type == constants.DT_BLOCK:
    block_driver, adopt = disk.logical_id
    ret = {
      "INSTANCE_DISK%d_DRIVER" % idx: block_driver,
      "INSTANCE_DISK%d_ID" % idx: adopt
      }
  elif disk.dev_type == constants.DT_RBD:
    rbd, name = disk.logical_id
    ret = {
      "INSTANCE_DISK%d_DRIVER" % idx: rbd,
      "INSTANCE_DISK%d_ID" % idx: name
      }
  elif disk.dev_type == constants.DT_EXT:
    provider, name = disk.logical_id
    ret = {
      "INSTANCE_DISK%d_PROVIDER" % idx: provider,
      "INSTANCE_DISK%d_ID" % idx: name
      }
  elif disk.dev_type == constants.DT_DRBD8:
    pnode, snode, port, pmin, smin, _ = disk.logical_id
    data, meta = disk.children
    data_vg, data_name = data.logical_id
    meta_vg, meta_name = meta.logical_id
    ret = {
      "INSTANCE_DISK%d_PNODE" % idx: pnode,
      "INSTANCE_DISK%d_SNODE" % idx: snode,
      "INSTANCE_DISK%d_PORT" % idx: port,
      "INSTANCE_DISK%d_PMINOR" % idx: pmin,
      "INSTANCE_DISK%d_SMINOR" % idx: smin,
      "INSTANCE_DISK%d_DATA_VG" % idx: data_vg,
      "INSTANCE_DISK%d_DATA_ID" % idx: data_name,
      "INSTANCE_DISK%d_META_VG" % idx: meta_vg,
      "INSTANCE_DISK%d_META_ID" % idx: meta_name,
      }
  elif disk.dev_type == constants.DT_GLUSTER:
    file_driver, name = disk.logical_id
    ret = {
      "INSTANCE_DISK%d_DRIVER" % idx: file_driver,
      "INSTANCE_DISK%d_ID" % idx: name
      }
  elif disk.dev_type == constants.DT_DISKLESS:
    ret = {}
  else:
    ret = {}

  ret.update({
    "INSTANCE_DISK%d_DEV_TYPE" % idx: disk.dev_type
    })

  return ret


def BuildDiskEnv(idx, disk):
  """Helper method to create disk's hooks env

  @type idx: integer
  @param idx: The index of the disk
  @type disk: L{objects.Disk} or dict
  @param disk: The disk object or a simple dict in case of LUInstanceCreate

  """
  ret = {}
  # In case of LUInstanceCreate this runs in CheckPrereq where lu.disks
  # is a list of dicts i.e the result of ComputeDisks
  if isinstance(disk, dict):
    uuid = disk.get("uuid", "")
    name = disk.get(constants.IDISK_NAME, "")
    size = disk.get(constants.IDISK_SIZE, "")
    mode = disk.get(constants.IDISK_MODE, "")
  elif isinstance(disk, objects.Disk):
    uuid = disk.uuid
    name = disk.name
    size = disk.size
    mode = disk.mode
    ret.update(BuildDiskLogicalIDEnv(idx, disk))

  # only name is optional here
  if name:
    ret["INSTANCE_DISK%d_NAME" % idx] = name
  ret["INSTANCE_DISK%d_UUID" % idx] = uuid
  ret["INSTANCE_DISK%d_SIZE" % idx] = size
  ret["INSTANCE_DISK%d_MODE" % idx] = mode

  return ret


def CheckInstanceExistence(lu, instance_name):
  """Raises an error if an instance with the given name exists already.

  @type instance_name: string
  @param instance_name: The name of the instance.

  To be used in the locking phase.

  """
  if instance_name in \
     [inst.name for inst in lu.cfg.GetAllInstancesInfo().values()]:
    raise errors.OpPrereqError("Instance '%s' is already in the cluster" %
                               instance_name, errors.ECODE_EXISTS)

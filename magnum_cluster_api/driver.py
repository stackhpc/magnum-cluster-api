# Copyright 2022 VEXXHOST Inc. All rights reserved.
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

import keystoneauth1
from magnum.drivers.common import driver

from magnum_cluster_api import clients, monitor, objects, resources, utils


class BaseDriver(driver.Driver):
    def __init__(self):
        self.k8s_api = clients.get_pykube_api()

    def _apply_cluster(self, context, cluster):
        """
        Apply or remove resources inside the management Kubernetes cluster using
        server side apply given a Magnum cluster.
        """
        if utils.get_cluster_label_as_bool(cluster, "auto_healing_enabled", True):
            resources.MachineHealthCheck(self.k8s_api, cluster).apply()
        else:
            resources.MachineHealthCheck(self.k8s_api, cluster).delete()

    def create_cluster(self, context, cluster, cluster_create_timeout):
        osc = clients.get_openstack_api(context)

        resources.Namespace(self.k8s_api).apply()

        resources.CloudControllerManagerConfigMap(self.k8s_api, cluster).apply()
        resources.CloudControllerManagerClusterResourceSet(
            self.k8s_api, cluster
        ).apply()

        resources.CalicoConfigMap(self.k8s_api, cluster).apply()
        resources.CalicoClusterResourceSet(self.k8s_api, cluster).apply()

        resources.CinderCSIConfigMap(self.k8s_api, cluster).apply()
        resources.CinderCSIClusterResourceSet(self.k8s_api, cluster).apply()

        credential = osc.keystone().client.application_credentials.create(
            user=cluster.user_id,
            name=cluster.uuid,
            description=f"Magnum cluster ({cluster.uuid})",
        )

        resources.CloudConfigSecret(
            self.k8s_api, cluster, osc.auth_url, osc.cinder_region_name(), credential
        ).apply()

        resources.ApiCertificateAuthoritySecret(self.k8s_api, cluster).apply()
        resources.EtcdCertificateAuthoritySecret(self.k8s_api, cluster).apply()
        resources.FrontProxyCertificateAuthoritySecret(self.k8s_api, cluster).apply()
        resources.ServiceAccountCertificateAuthoritySecret(
            self.k8s_api, cluster
        ).apply()

        for node_group in cluster.nodegroups:
            self.create_nodegroup(context, cluster, node_group, credential=credential)

        resources.OpenStackCluster(self.k8s_api, cluster, context).apply()
        resources.Cluster(self.k8s_api, cluster).apply()

        self._apply_cluster(context, cluster)

    def update_cluster_status(self, context, cluster, use_admin_ctx=False):
        osc = clients.get_openstack_api(context)

        capi_cluster = resources.Cluster(self.k8s_api, cluster).get_object()

        if cluster.status in (
            "CREATE_IN_PROGRESS",
            "UPDATE_IN_PROGRESS",
        ):
            capi_cluster.reload()
            status_map = {
                c["type"]: c["status"] for c in capi_cluster.obj["status"]["conditions"]
            }

            # health_status
            # node_addresses
            # master_addreses
            # discovery_url = ???
            # docker_volume_size
            # container_version
            # health_status_reason

            if status_map.get("ControlPlaneReady") != "True":
                return

            api_endpoint = capi_cluster.obj["spec"]["controlPlaneEndpoint"]
            cluster.api_address = (
                f"https://{api_endpoint['host']}:{api_endpoint['port']}"
            )

            for node_group in cluster.nodegroups:
                ng = self.update_nodegroup_status(context, cluster, node_group)
                if ng.status not in (
                    "CREATE_COMPLETE",
                    "UPDATE_COMPLETE",
                ):
                    return

                if node_group.role == "master":
                    kcp = resources.KubeadmControlPlane(
                        self.k8s_api, cluster, node_group
                    ).get_object()
                    kcp.reload()

                    cluster.coe_version = kcp.obj["status"]["version"]

            if cluster.status == "CREATE_IN_PROGRESS":
                cluster.status = "CREATE_COMPLETE"

            if cluster.status == "UPDATE_IN_PROGRESS":
                cluster.status = "UPDATE_COMPLETE"

            cluster.save()

        if cluster.status == "DELETE_IN_PROGRESS":
            if capi_cluster.exists():
                return

            # NOTE(mnaser): We delete the application credentials at this stage
            #               to make sure CAPI doesn't lose access to OpenStack.
            try:
                osc.keystone().client.application_credentials.find(
                    name=cluster.uuid,
                    user=cluster.user_id,
                ).delete()
            except keystoneauth1.exceptions.http.NotFound:
                pass

            resources.CloudConfigSecret(self.k8s_api, cluster).delete()
            resources.ApiCertificateAuthoritySecret(self.k8s_api, cluster).delete()
            resources.EtcdCertificateAuthoritySecret(self.k8s_api, cluster).delete()
            resources.FrontProxyCertificateAuthoritySecret(
                self.k8s_api, cluster
            ).delete()
            resources.ServiceAccountCertificateAuthoritySecret(
                self.k8s_api, cluster
            ).delete()

            cluster.status = "DELETE_COMPLETE"
            cluster.save()

    def update_cluster(self, context, cluster, scale_manager=None, rollback=False):
        raise NotImplementedError()

    def resize_cluster(
        self,
        context,
        cluster,
        resize_manager,
        node_count,
        nodes_to_remove,
        nodegroup=None,
    ):
        if nodegroup is None:
            nodegroup = cluster.default_ng_worker

        if nodes_to_remove:
            machines = objects.Machine.objects(self.k8s_api).filter(
                namespace="magnum-system",
                selector={
                    "cluster.x-self.k8s_api.io/deployment-name": resources.name_from_node_group(
                        cluster, nodegroup
                    )
                },
            )

            for machine in machines:
                instance_uuid = machine.obj["spec"]["providerID"].split("/")[-1]
                if instance_uuid in nodes_to_remove:
                    machine.obj["metadata"].setdefault("annotations", {})
                    machine.obj["metadata"]["annotations"][
                        "cluster.x-self.k8s_api.io/delete-machine"
                    ] = "yes"
                    machine.update()

        nodegroup.node_count = node_count
        self.update_nodegroup(context, cluster, nodegroup)

    def upgrade_cluster(
        self,
        context,
        cluster,
        cluster_template,
        max_batch_size,
        nodegroup,
        scale_manager=None,
        rollback=False,
    ):
        raise NotImplementedError()

    def delete_cluster(self, context, cluster):
        resources.Cluster(self.k8s_api, cluster).delete()

    def create_nodegroup(self, context, cluster, nodegroup, credential=None):
        osc = clients.get_openstack_api(context)

        resources.OpenStackMachineTemplate(
            self.k8s_api, cluster, nodegroup, context
        ).apply()

        if nodegroup.role == "master":
            resources.KubeadmControlPlane(
                self.k8s_api,
                cluster,
                nodegroup,
                auth_url=osc.auth_url,
                region_name=osc.cinder_region_name(),
                credential=credential,
            ).apply()
        else:
            resources.KubeadmConfigTemplate(
                self.k8s_api,
                cluster,
                auth_url=osc.auth_url,
                region_name=osc.cinder_region_name(),
                credential=credential,
            ).apply()
            resources.MachineDeployment(self.k8s_api, cluster, nodegroup).apply()

    def update_nodegroup_status(self, context, cluster, nodegroup):
        action = nodegroup.status.split("_")[0]

        if nodegroup.role == "master":
            kcp = resources.KubeadmControlPlane(
                self.k8s_api, cluster, nodegroup
            ).get_object()
            kcp.reload()

            ready = kcp.obj["status"].get("ready", False)
            failure_message = kcp.obj["status"].get("failureMessage")

            if ready:
                nodegroup.status = f"{action}_COMPLETE"
            nodegroup.status_reason = failure_message
        else:
            md = resources.MachineDeployment(
                self.k8s_api, cluster, nodegroup
            ).get_object()
            md.reload()

            phase = md.obj["status"]["phase"]

            if phase in ("ScalingUp", "ScalingDown"):
                nodegroup.status = f"{action}_IN_PROGRESS"
            elif phase == "Running":
                nodegroup.status = f"{action}_COMPLETE"
            elif phase in ("Failed", "Unknown"):
                nodegroup.status = f"{action}_FAILED"

        nodegroup.save()

        return nodegroup

    def update_nodegroup(self, context, cluster, nodegroup):
        resources.MachineDeployment(self.k8s_api, cluster, nodegroup).apply()

    def delete_nodegroup(self, context, cluster, nodegroup):
        if nodegroup.role != "master":
            resources.MachineDeployment(self.k8s_api, cluster, nodegroup).delete()
            resources.KubeadmConfigTemplate(self.k8s_api, cluster).delete()

        resources.OpenStackMachineTemplate(
            self.k8s_api, cluster, nodegroup, context
        ).delete()

    def get_monitor(self, context, cluster):
        return monitor.ClusterApiMonitor(context, cluster)

    # def rotate_ca_certificate(self, context, cluster):
    #     raise exception.NotSupported(
    #         "'rotate_ca_certificate' is not supported by this driver.")

    def create_federation(self, context, federation):
        raise NotImplementedError()

    def update_federation(self, context, federation):
        raise NotImplementedError()

    def delete_federation(self, context, federation):
        raise NotImplementedError()


class UbuntuFocalDriver(BaseDriver):
    @property
    def provides(self):
        return [
            {"server_type": "vm", "os": "ubuntu-focal", "coe": "kubernetes"},
        ]

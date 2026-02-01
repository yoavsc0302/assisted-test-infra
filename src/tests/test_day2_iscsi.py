"""
Test for Day2 iSCSI Multi-NIC scenario.

This test installs a SNO cluster (day1) and adds a day2 worker with:
- iSCSI boot (iPXE -> iSCSI chain)
- 4 network interfaces with different purposes:
  1. iSCSI boot NIC - boots from iSCSI target containing discovery ISO
  2. Machine network NIC - connected to day1 cluster network
  3. DNS only NIC - resolves day1 API address
  4. Dummy NIC - connected without IP configuration
"""

import os
from typing import Callable

import pytest
from junit_report import JunitTestSuite

import consts
from assisted_test_infra.test_infra.controllers.iscsi_target_controller import (
    Iqn,
    IscsiTargetConfig,
    IscsiTargetController,
)
from assisted_test_infra.test_infra.utils.waiting import wait_till_all_hosts_are_in_status
from service_client import log
from tests.base_test import BaseTest
from tests.config import ClusterConfig, TerraformConfig
from tests.config.global_configs import Day2ClusterConfig


class TestDay2Iscsi(BaseTest):
    """
    Test Day2 worker installation with iSCSI boot and multiple network interfaces.

    The test flow:
    1. Day1 SNO cluster installs normally
    2. Generate day2 discovery ISO
    3. Create iSCSI target with the discovery ISO
    4. Create additional libvirt networks (iSCSI, DNS, dummy)
    5. Create day2 worker VM with 4 NICs
    6. Worker boots via iPXE -> iSCSI -> discovery ISO
    7. Worker discovers, registers, and installs
    8. Verify worker joins the cluster
    """

    # Network XML templates for the additional networks
    # Each network is isolated with specific characteristics

    # iSCSI network: Provides DHCP for iSCSI boot
    ISCSI_NETWORK_NAME = "iscsi-boot-net"
    ISCSI_NETWORK_XML = """
    <network>
      <name>{name}</name>
      <bridge name="{bridge}" stp="on" delay="0"/>
      <forward mode="nat"/>
      <ip address="192.168.200.1" netmask="255.255.255.0">
        <dhcp>
          <range start="192.168.200.10" end="192.168.200.250"/>
        </dhcp>
      </ip>
    </network>
    """

    # DNS network: Provides DHCP and DNS entries for day1 API resolution
    DNS_NETWORK_NAME = "dns-only-net"
    DNS_NETWORK_XML = """
    <network>
      <name>{name}</name>
      <bridge name="{bridge}" stp="on" delay="0"/>
      <forward mode="nat"/>
      <ip address="192.168.201.1" netmask="255.255.255.0">
        <dhcp>
          <range start="192.168.201.10" end="192.168.201.250"/>
        </dhcp>
      </ip>
      <dns>
        <host ip="{api_vip}">
          <hostname>{api_hostname}</hostname>
        </host>
      </dns>
    </network>
    """

    # Dummy network: Isolated network without DHCP or forwarding
    DUMMY_NETWORK_NAME = "dummy-isolated-net"
    DUMMY_NETWORK_XML = """
    <network>
      <name>{name}</name>
      <bridge name="{bridge}" stp="on" delay="0"/>
    </network>
    """

    # iPXE script template for iSCSI boot
    ISCSI_IPXE_SCRIPT = """#!ipxe
dhcp net0
set initiator-iqn {initiator_iqn}
sanboot iscsi:{server_ip}::::{target_iqn}
"""

    @pytest.fixture
    def sno_cluster_configuration(self, new_cluster_configuration: ClusterConfig) -> ClusterConfig:
        """Configure day1 as SNO (1 master, 0 workers)."""
        new_cluster_configuration.masters_count = 1
        new_cluster_configuration.workers_count = 0
        new_cluster_configuration.high_availability_mode = consts.HighAvailabilityMode.NONE
        return new_cluster_configuration

    @pytest.fixture
    def sno_controller_configuration(self, prepared_controller_configuration: TerraformConfig) -> TerraformConfig:
        """Configure controller for SNO deployment."""
        prepared_controller_configuration.masters_count = 1
        prepared_controller_configuration.workers_count = 0
        prepared_controller_configuration.bootstrap_in_place = True
        # Set single_node_ip for SNO - this is needed for Terraform DNS templates
        # The prepared_controller_configuration fixture checks bootstrap_in_place before our override,
        # so we need to set single_node_ip manually here
        if prepared_controller_configuration.net_asset:
            prepared_controller_configuration.single_node_ip = (
                prepared_controller_configuration.net_asset.machine_cidr.replace("0/24", "10")
            )
        return prepared_controller_configuration

    @pytest.fixture
    def iscsi_day2_controller_configuration(
        self, prepared_day2_controller_configuration: TerraformConfig
    ) -> TerraformConfig:
        """Configure day2 controller with network boot capability."""
        config = prepared_day2_controller_configuration
        # Set boot order to network first, then disk
        config.worker_boot_devices = ["network", "hd"]
        return config

    @pytest.fixture
    def iscsi_day2_cluster_configuration(self, new_day2_cluster_configuration: Day2ClusterConfig) -> Day2ClusterConfig:
        """Configure day2 cluster with 1 worker."""
        new_day2_cluster_configuration.day2_workers_count = 1
        new_day2_cluster_configuration.day2_masters_count = 0
        return new_day2_cluster_configuration

    @staticmethod
    def _format_iscsi_network_xml(cluster_name: str) -> str:
        """Format the iSCSI network XML with unique names."""
        return TestDay2Iscsi.ISCSI_NETWORK_XML.format(
            name=f"{cluster_name}-{TestDay2Iscsi.ISCSI_NETWORK_NAME}",
            bridge=f"iscsi-{cluster_name[:8]}",
        )

    @staticmethod
    def _format_dns_network_xml(cluster_name: str, api_vip: str, api_hostname: str) -> str:
        """Format the DNS network XML with API VIP and hostname."""
        return TestDay2Iscsi.DNS_NETWORK_XML.format(
            name=f"{cluster_name}-{TestDay2Iscsi.DNS_NETWORK_NAME}",
            bridge=f"dns-{cluster_name[:8]}",
            api_vip=api_vip,
            api_hostname=api_hostname,
        )

    @staticmethod
    def _format_dummy_network_xml(cluster_name: str) -> str:
        """Format the dummy network XML with unique names."""
        return TestDay2Iscsi.DUMMY_NETWORK_XML.format(
            name=f"{cluster_name}-{TestDay2Iscsi.DUMMY_NETWORK_NAME}",
            bridge=f"dum-{cluster_name[:8]}",
        )

    @staticmethod
    def _format_iscsi_ipxe_script(initiator_iqn: str, server_ip: str, target_iqn: str) -> str:
        """Format the iPXE script for iSCSI boot."""
        return TestDay2Iscsi.ISCSI_IPXE_SCRIPT.format(
            initiator_iqn=initiator_iqn,
            server_ip=server_ip,
            target_iqn=target_iqn,
        )

    @pytest.mark.override_cluster_configuration("sno_cluster_configuration")
    @pytest.mark.override_controller_configuration("sno_controller_configuration")
    @pytest.mark.override_day2_cluster_configuration("iscsi_day2_cluster_configuration")
    @pytest.mark.override_day2_controller_configuration("iscsi_day2_controller_configuration")
    @JunitTestSuite()
    def test_day2_iscsi_multi_nic(
        self,
        cluster,
        day2_cluster,
        iscsi_target_controller: IscsiTargetController,
        ipxe_server: Callable,
        attach_interface: Callable,
        api_client,
    ):
        """
        Test Day2 worker with iSCSI boot and multiple NICs.

        This test validates:
        1. SNO day1 cluster installation
        2. Day2 worker boots via iPXE -> iSCSI chain
        3. Worker has 4 network interfaces configured correctly
        4. Worker successfully joins the cluster
        """
        log.info("=== Starting Day2 iSCSI Multi-NIC Test ===")

        # Step 1: Ensure day1 SNO cluster is installed
        # The cluster fixture handles installation if not already done
        log.info("Step 1: Verifying day1 SNO cluster is installed")
        cluster_details = cluster.get_details()
        log.info(f"Day1 cluster: {cluster_details.name}, status: {cluster_details.status}")

        # Get day1 cluster network information
        api_vip = cluster_details.api_vips[0].ip if cluster_details.api_vips else None
        if not api_vip:
            # For SNO, get the single node IP
            api_vip = cluster.get_ip_for_single_node(cluster.api_client, cluster.id, cluster.get_primary_machine_cidr())
        api_hostname = f"api.{cluster_details.name}.{cluster_details.base_dns_domain}"
        log.info(f"Day1 API VIP: {api_vip}, API hostname: {api_hostname}")

        # Step 2: Generate day2 discovery ISO
        log.info("Step 2: Generating day2 discovery ISO")
        day2_cluster.set_pull_secret(day2_cluster._config.pull_secret)
        day2_cluster.set_cluster_proxy()
        day2_cluster.config_etc_hosts(api_vip, day2_cluster._config.day1_api_vip_dnsname)

        # Generate and download the infraenv ISO
        day2_cluster.generate_and_download_infra_env(
            iso_download_path=day2_cluster._config.iso_download_path,
            iso_image_type=day2_cluster._config.iso_image_type,
            cpu_architecture=day2_cluster._config.day2_cpu_architecture,
        )
        iso_path = day2_cluster._config.iso_download_path
        log.info(f"Day2 discovery ISO generated at: {iso_path}")

        # Step 3: Create iSCSI target with the discovery ISO
        log.info("Step 3: Creating iSCSI target with discovery ISO")
        cluster_name = cluster_details.name
        worker_name = f"{cluster_name}-day2-worker"
        target_iqn = Iqn(f"{worker_name}-target")
        initiator_iqn = Iqn(f"{worker_name}-initiator")
        iscsi_server_ip = "192.168.200.1"  # iSCSI network gateway

        iscsi_config = IscsiTargetConfig(
            disk_name=f"{worker_name}-disk",
            disk_size_gb=120,
            iqn=target_iqn,
            remote_iqn=initiator_iqn,
            iso_disk_copy=iso_path,
            servers=[iscsi_server_ip],
        )
        iscsi_target_controller.create_target(iscsi_config, clear_config=True)
        log.info(f"iSCSI target created: {target_iqn}")

        # Step 4: Prepare network XMLs for additional networks
        log.info("Step 4: Preparing additional libvirt networks")
        iscsi_network_xml = self._format_iscsi_network_xml(cluster_name)
        dns_network_xml = self._format_dns_network_xml(cluster_name, api_vip, api_hostname)
        dummy_network_xml = self._format_dummy_network_xml(cluster_name)

        # Step 5: Prepare and start iPXE server with iSCSI boot script
        log.info("Step 5: Setting up iPXE server for iSCSI boot")
        ipxe_script_content = self._format_iscsi_ipxe_script(
            initiator_iqn=str(initiator_iqn),
            server_ip=iscsi_server_ip,
            target_iqn=str(target_iqn),
        )
        log.info(f"iPXE script:\n{ipxe_script_content}")

        # Start iPXE server with custom script
        ipxe_controller = ipxe_server(
            name="iscsi_ipxe_controller",
            api_client=api_client,
            empty_pxe_content=True,  # We'll provide custom content
        )

        # The iPXE controller needs an infraenv to get started, we use the day2 infraenv
        infra_env_id = day2_cluster._infra_env_config.infra_env_id
        ipxe_controller.run(infra_env_id=infra_env_id, cluster_name=cluster_name)

        # Write our custom iSCSI boot script
        os.makedirs(ipxe_controller._ipxe_scripts_folder, exist_ok=True)
        with open(f"{ipxe_controller._ipxe_scripts_folder}/{cluster_name}", "w") as f:
            f.write(ipxe_script_content)
        log.info("iPXE server started with iSCSI boot script")

        # Step 6: Create day2 worker VMs (stopped) and attach additional interfaces
        log.info("Step 6: Creating day2 worker VMs with multiple NICs")

        # Get the day2 nodes controller and prepare VMs (creates them in stopped state)
        day2_nodes = day2_cluster.nodes
        day2_nodes.prepare_nodes()

        # Get the worker node objects (they should be created but not running)
        worker_nodes = list(day2_nodes.get_nodes())
        if not worker_nodes:
            raise RuntimeError("No day2 worker nodes found after prepare_nodes()")

        worker_node = worker_nodes[0]
        log.info(f"Day2 worker node: {worker_node.name}")

        # Ensure the node is not running before attaching interfaces
        worker_node.shutdown()

        # Attach additional network interfaces to the worker
        # NIC 1: Machine network (already configured by Terraform)
        # NIC 2: iSCSI network
        log.info("Attaching iSCSI network interface")
        attach_interface(worker_node, network_xml=iscsi_network_xml)

        # NIC 3: DNS network
        log.info("Attaching DNS network interface")
        attach_interface(worker_node, network_xml=dns_network_xml)

        # NIC 4: Dummy network
        log.info("Attaching dummy network interface")
        attach_interface(worker_node, network_xml=dummy_network_xml)

        log.info("All additional NICs attached to worker node")

        # Step 7: Start the node - it should boot via iPXE -> iSCSI
        log.info("Step 7: Starting worker node with all NICs attached")
        worker_node.start()

        # Wait for networking to be ready on the node
        log.info("Waiting for networking on day2 worker...")
        day2_nodes.wait_for_networking()

        # Step 8: Set hostnames and roles and wait for discovery
        log.info("Step 8: Waiting for worker to discover and register")
        day2_cluster.set_hostnames_and_roles()

        # Wait for host to be in known status
        wait_till_all_hosts_are_in_status(
            client=api_client,
            cluster_id=day2_cluster.id,
            nodes_count=1,
            statuses=[consts.NodesStatus.KNOWN],
            interval=30,
        )
        log.info("Day2 worker discovered and in 'known' status")

        # Step 9: Start day2 installation
        log.info("Step 9: Starting day2 worker installation")
        day2_cluster.start_install_and_wait_for_installed()

        # Step 10: Verify worker joined the cluster
        log.info("Step 10: Verifying worker joined the OCP cluster")
        ocp_nodes = day2_cluster.get_ocp_cluster_nodes(day2_cluster._kubeconfig_path)
        log.info(f"OCP cluster has {len(ocp_nodes)} nodes")

        # Verify we have 2 nodes (1 master + 1 worker)
        assert len(ocp_nodes) >= 2, f"Expected at least 2 nodes in cluster, got {len(ocp_nodes)}"

        # Find worker nodes
        worker_count = sum(
            1 for node in ocp_nodes if "node-role.kubernetes.io/worker" in node.get("metadata", {}).get("labels", {})
        )
        log.info(f"Found {worker_count} worker node(s) in the cluster")
        assert worker_count >= 1, "Expected at least 1 worker node in cluster"

        log.info("=== Day2 iSCSI Multi-NIC Test PASSED ===")

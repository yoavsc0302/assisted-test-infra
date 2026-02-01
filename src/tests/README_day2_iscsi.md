# Day2 iSCSI Multi-NIC Test

Test for adding a day2 worker to a SNO cluster with iSCSI boot and multiple network interfaces.

## Architecture

```mermaid
graph TB
    subgraph "Day1 SNO Cluster"
        MASTER[Master Node]
        API[API Server]
    end

    subgraph "Day2 Worker VM"
        NIC1[NIC 1<br/>Machine Network]
        NIC2[NIC 2<br/>iSCSI Boot]
        NIC3[NIC 3<br/>DNS Only]
        NIC4[NIC 4<br/>Dummy]
    end

    subgraph "Infrastructure"
        IPXE[iPXE Server]
        ISCSI[iSCSI Target<br/>Discovery ISO]
        DNS[DNS Network<br/>API Resolution]
    end

    NIC1 -->|DHCP| MASTER
    NIC2 -->|sanboot| ISCSI
    NIC3 -->|DNS| DNS
    DNS -.->|resolves| API
    IPXE -->|boot script| NIC2
```

## Boot Sequence

```mermaid
sequenceDiagram
    participant VM as Day2 Worker VM
    participant IPXE as iPXE Server
    participant ISCSI as iSCSI Target
    participant ISO as Discovery ISO
    participant SVC as Assisted Service
    participant OCP as OCP Cluster

    VM->>IPXE: PXE boot request
    IPXE->>VM: iSCSI boot script
    VM->>ISCSI: Connect to target
    ISCSI->>VM: Boot from discovery ISO
    VM->>SVC: Register host
    SVC->>VM: Installation instructions
    VM->>VM: Install RHCOS
    VM->>OCP: Join cluster
    OCP->>VM: CSR approved
```

## API Sequence

```mermaid
sequenceDiagram
    participant Test as Test Runner
    participant API as Assisted Service API
    participant Master as SNO Master
    participant Worker as Day2 Worker
    participant OCP as OCP Cluster

    Note over Test,Master: Day1 SNO Cluster Creation
    Test->>API: POST /v2/clusters (create SNO cluster)
    API-->>Test: cluster_id
    Test->>API: POST /v2/infra-envs (create infraenv)
    API-->>Test: infra_env_id, ISO
    Test->>Master: Boot from discovery ISO
    Master->>API: Register host + inventory
    API-->>Master: host_id

    Note over Test,Master: Day1 Installation
    Test->>API: POST /v2/clusters/{id}/install
    API-->>Test: 202 Accepted
    Master->>Master: Bootstrap-in-place install
    Master->>API: PUT status (installed)
    API-->>Test: Cluster ready

    Note over Test,Worker: Day2 Cluster Setup
    Test->>API: POST /v2/clusters (create day2 cluster)
    API-->>Test: day2_cluster_id
    Test->>API: POST /v2/infra-envs (day2 infraenv)
    API-->>Test: day2_infra_env_id, ISO
    Test->>Test: Create iSCSI target with ISO

    Note over Worker,API: Day2 Worker Discovery
    Test->>Worker: Boot via iPXE -> iSCSI
    Worker->>API: Register host + inventory
    API-->>Worker: host_id
    Test->>API: GET /v2/clusters/{id}/hosts
    API-->>Test: hosts (status: known)

    Note over Test,Worker: Day2 Installation
    Test->>API: POST /v2/infra-envs/{id}/hosts/{id}/install
    API-->>Test: 202 Accepted
    Worker->>Worker: Install RHCOS
    Worker->>API: PUT status (installed)

    Note over Worker,OCP: Cluster Join
    Worker->>OCP: kubelet CSR request
    Test->>OCP: oc adm certificate approve
    OCP-->>Worker: CSR approved
    Worker->>OCP: Node Ready (2 nodes total)
```

## Test Class

`TestDay2Iscsi` in `src/tests/test_day2_iscsi.py`

Extends `BaseTest` and uses existing fixtures: `iscsi_target_controller`, `ipxe_server`, `attach_interface`, `cluster`, `day2_cluster`.

## Fixtures

| Fixture | Purpose |
|---------|---------|
| `sno_cluster_configuration` | Day1 as SNO (1 master, 0 workers, HA mode: none) |
| `sno_controller_configuration` | Bootstrap-in-place enabled for SNO |
| `iscsi_day2_controller_configuration` | Boot order: `["network", "hd"]` |
| `iscsi_day2_cluster_configuration` | 1 day2 worker, 0 day2 masters |

## Network Templates

| Network | CIDR | DHCP | Purpose |
|---------|------|------|---------|
| iSCSI | `192.168.200.0/24` | Yes | iSCSI boot network with NAT |
| DNS | `192.168.201.0/24` | Yes | DNS entries for day1 API resolution |
| Dummy | N/A | No | Isolated, no IP configuration |
| Machine | (day1 network) | Yes | Already configured by Terraform |

## Test Flow

1. Day1 SNO cluster installed (via `cluster` fixture)
2. Generate day2 discovery ISO
3. Create iSCSI target with discovery ISO
4. Set up iPXE server with custom iSCSI boot script
5. Create day2 worker VMs (stopped)
6. Attach 3 additional NICs (iSCSI, DNS, dummy)
7. Start worker - boots via iPXE -> iSCSI -> discovery ISO
8. Wait for discovery and registration
9. Install day2 worker
10. Verify worker joined the OCP cluster

## iPXE Boot Script

```ipxe
#!ipxe
dhcp net0
set initiator-iqn {initiator_iqn}
sanboot iscsi:{server_ip}::::{target_iqn}
```

## Running the Test

```bash
export OPENSHIFT_VERSION=4.20
export NUM_MASTERS=1
export NUM_WORKERS=0
export NUM_DAY2_WORKERS=1
export TEST_TEARDOWN=false  # Optional: keep resources for debugging
make test TEST=./src/tests/test_day2_iscsi.py TEST_FUNC=test_day2_iscsi_multi_nic
```

## Verification

After the test runs, verify:

```bash
# Check VMs
virsh list --all  # Should show SNO master + day2 worker

# Check networks
virsh net-list  # Should show 4+ networks

# Check iSCSI target
targetcli ls  # Should show configured target

# Check cluster nodes
oc get nodes  # Should show 2 nodes (1 master + 1 worker)
```

## Accessing Logs

The test includes 27 `log.info()` statements for debugging. Access logs in these locations:

**Console output (real-time)**
```bash
# Logs appear directly in terminal when running
make test TEST=./src/tests/test_day2_iscsi.py TEST_FUNC=test_day2_iscsi_multi_nic
```

**Reports directory**
```bash
# Test reports and logs stored in:
./reports/

# View the latest test log
ls -la ./reports/
```

**Download cluster logs (on failure)**
```bash
make download_logs          # Assisted-service logs
make download_cluster_logs  # Cluster-specific logs
```

**Service logs (kind/minikube)**
```bash
kubectl logs -n assisted-installer deployment/assisted-service
```

**Virsh/libvirt logs (VM console)**
```bash
virsh console <vm-name>     # VM console output
journalctl -u libvirtd      # Libvirt logs
```

## Dependencies

- `targetcli` installed on hypervisor for iSCSI target management
- iPXE-enabled network boot support in libvirt
- Pull secret configured (`PULL_SECRET` or `PULL_SECRET_FILE`)

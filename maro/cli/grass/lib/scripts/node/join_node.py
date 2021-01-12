# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.


import argparse
import functools
import json
import operator
import os
import pathlib
import pwd
import shutil
import subprocess
import sys

import deepdiff
import requests
import yaml

# Commands

CREATE_DOCKER_USER_COMMAND = """\
sudo groupadd docker
sudo gpasswd -a {node_username} docker
"""

SETUP_SAMBA_MOUNT_COMMAND = """\
mkdir -p {maro_shared_path}
sudo mount -t cifs -o username={master_username},password={samba_password} //{master_hostname}/sambashare {maro_shared_path}
echo '//{master_hostname}/sambashare  {maro_shared_path} cifs  username={master_username},password={samba_password}  0  0' | \
    sudo tee -a /etc/fstab
"""

START_NODE_AGENT_SERVICE_COMMAND = """\
systemctl --user daemon-reload
systemctl --user start maro-node-agent.service
systemctl --user enable maro-node-agent.service
loginctl enable-linger {node_username}  # Make sure the user is not logged out
"""

START_NODE_API_SERVER_SERVICE_COMMAND = """\
systemctl --user daemon-reload
systemctl --user start maro-node-api-server.service
systemctl --user enable maro-node-api-server.service
loginctl enable-linger {node_username}  # Make sure the user is not logged out
"""


class Params:
    DEFAULT_SSH_PORT = 22
    DEFAULT_API_SERVER_PORT = 51812


class Paths:
    MARO_SHARED = "~/.maro-shared"
    MARO_LOCAL = "~/.maro-local"

    ABS_MARO_SHARED = os.path.expanduser(MARO_SHARED)
    ABS_MARO_LOCAL = os.path.expanduser(MARO_LOCAL)


class NodeJoiner:
    def __init__(self, join_node_deployment: dict):
        self.join_node_deployment = join_node_deployment

        master_api_client = MasterApiClientV1(
            master_hostname=join_node_deployment["master"]["hostname"],
            api_server_port=join_node_deployment["master"]["api_server"]["port"]
        )
        self.node_details = master_api_client.create_node(node_details=join_node_deployment["node"])

        self.cluster_details = master_api_client.get_cluster()
        self.master_details = master_api_client.get_master()

    @staticmethod
    def create_docker_user():
        command = CREATE_DOCKER_USER_COMMAND.format(node_username=pwd.getpwuid(os.getuid()).pw_name)
        Subprocess.run(command=command)

    def setup_samba_mount(self):
        command = SETUP_SAMBA_MOUNT_COMMAND.format(
            master_username=self.master_details["username"],
            master_hostname=self.master_details["hostname"],
            samba_password=self.master_details["samba"]["password"],
            maro_shared_path=Paths.ABS_MARO_SHARED
        )
        Subprocess.run(command=command)

    def start_node_agent_service(self):
        # Dump node_agent.config
        os.makedirs(name=f"{Paths.ABS_MARO_LOCAL}/services", exist_ok=True)
        with open(f"{Paths.ABS_MARO_LOCAL}/services/maro-node-agent.config", "w") as fw:
            json.dump(
                obj={
                    "cluster_name": self.cluster_details["name"],
                    "node_name": self.node_details["name"],
                    "master_hostname": self.master_details["hostname"],
                    "redis_port": self.master_details["redis"]["port"]
                },
                fp=fw
            )

        # Load .service
        with open(
            file=f"{Paths.ABS_MARO_SHARED}/lib/grass/services/node_agent/maro-node-agent.service",
            mode="r"
        ) as fr:
            service_file = fr.read()

        # Rewrite data in .service and write it to systemd folder
        service_file = service_file.format(home_path=str(pathlib.Path.home()))
        os.makedirs(name=os.path.expanduser("~/.config/systemd/user/"), exist_ok=True)
        with open(file=os.path.expanduser("~/.config/systemd/user/maro-node-agent.service"), mode="w") as fw:
            fw.write(service_file)

        command = START_NODE_AGENT_SERVICE_COMMAND.format(node_username=pwd.getpwuid(os.getuid()).pw_name)
        Subprocess.run(command=command)

    def start_node_api_server_service(self):
        # Load .service
        with open(
            file=f"{Paths.ABS_MARO_SHARED}/lib/grass/services/node_api_server/maro-node-api-server.service",
            mode="r"
        ) as fr:
            service_file = fr.read()

        # Rewrite data in .service and write it to systemd folder
        service_file = service_file.format(
            home_path=str(pathlib.Path.home()),
            api_server_port=self.cluster_details["connection"]["api_server"]["port"]
        )
        os.makedirs(os.path.expanduser("~/.config/systemd/user/"), exist_ok=True)
        with open(file=os.path.expanduser("~/.config/systemd/user/maro-node-api-server.service"), mode="w") as fw:
            fw.write(service_file)

        command = START_NODE_API_SERVER_SERVICE_COMMAND.format(node_username=pwd.getpwuid(os.getuid()).pw_name)
        Subprocess.run(command=command)

    @staticmethod
    def copy_leave_script():
        os.makedirs(name=f"{Paths.ABS_MARO_LOCAL}/scripts", exist_ok=True)
        shutil.copy2(
            src=f"{Paths.ABS_MARO_SHARED}/lib/grass/scripts/node/leave.py",
            dst=f"{Paths.ABS_MARO_LOCAL}/scripts"
        )

    # Utils

    @staticmethod
    def standardize_join_node_deployment(join_node_deployment: dict) -> dict:
        join_node_deployment_template = {
            "mode": "",
            "master": {
                "hostname": "",
            },
            "node": {
                "hostname": "",
                "public_ip_address": "",
                "private_ip_address": "",
                "resources": {
                    "cpu": "",
                    "memory": "",
                    "gpu": ""
                }
            }
        }
        DeploymentValidator.validate_and_fill_dict(
            template_dict=join_node_deployment_template,
            actual_dict=join_node_deployment,
            optional_key_to_value={
                "root['master']['api_server']": {"port": Params.DEFAULT_API_SERVER_PORT},
                "root['master']['api_server']['port']": Params.DEFAULT_API_SERVER_PORT,
                "root['node']['api_server']": {"port": Params.DEFAULT_API_SERVER_PORT},
                "root['node']['api_server']['port']": Params.DEFAULT_API_SERVER_PORT,
                "root['node']['ssh']": {"port": Params.DEFAULT_SSH_PORT},
                "root['node']['ssh']['port']": Params.DEFAULT_SSH_PORT
            }
        )

        return join_node_deployment


class MasterApiClientV1:
    def __init__(self, master_hostname: str, api_server_port: int):
        self.master_api_server_url_prefix = f"http://{master_hostname}:{api_server_port}/v1"

    # Cluster related.

    def get_cluster(self) -> dict:
        response = requests.get(url=f"{self.master_api_server_url_prefix}/cluster")
        return response.json()

    # Master related.

    def get_master(self):
        response = requests.get(url=f"{self.master_api_server_url_prefix}/master")
        return response.json()

    # Node related.

    def list_nodes(self) -> list:
        response = requests.get(url=f"{self.master_api_server_url_prefix}/nodes")
        return response.json()

    def get_name_to_node_details(self) -> dict:
        nodes_details = self.list_nodes()
        name_to_node_details = {}
        for node_details in nodes_details:
            name_to_node_details[node_details["name"]] = node_details
        return name_to_node_details

    def create_node(self, node_details: dict) -> dict:
        response = requests.post(url=f"{self.master_api_server_url_prefix}/nodes", json=node_details)
        return response.json()


class DeploymentValidator:
    @staticmethod
    def validate_and_fill_dict(template_dict: dict, actual_dict: dict, optional_key_to_value: dict) -> None:
        """Validate incoming actual_dict with template_dict, and fill optional keys to the template.

        We use deepDiff to find missing keys in the actual_dict, see
        https://deepdiff.readthedocs.io/en/latest/diff.html#deepdiff-reference for reference.

        Args:
            template_dict (dict): template dict, we only need the layer structure of keys here, and ignore values.
            actual_dict (dict): the actual dict with values, may miss some keys.
            optional_key_to_value (dict): mapping of optional keys to values.

        Returns:
            None.
        """
        deep_diff = deepdiff.DeepDiff(template_dict, actual_dict).to_dict()

        missing_key_strs = deep_diff.get("dictionary_item_removed", [])
        for missing_key_str in missing_key_strs:
            if missing_key_str not in optional_key_to_value:
                raise Exception(f"Key '{missing_key_str}' not found.")
            else:
                DeploymentValidator._set_value(
                    original_dict=actual_dict,
                    key_list=DeploymentValidator._get_parent_to_child_key_list(deep_diff_str=missing_key_str),
                    value=optional_key_to_value[missing_key_str]
                )

    @staticmethod
    def _set_value(original_dict: dict, key_list: list, value) -> None:
        """Set the value to the original dict based on the key_list.

        Args:
            original_dict (dict): original dict that needs to be modified.
            key_list (list): the parent to child path of keys, which describes that position of the value.
            value: the value needs to be set.

        Returns:
            None.
        """
        DeploymentValidator._get_sub_structure_of_dict(original_dict, key_list[:-1])[key_list[-1]] = value

    @staticmethod
    def _get_parent_to_child_key_list(deep_diff_str: str) -> list:
        """Get parent to child key list by parsing the deep_diff_str.

        Args:
            deep_diff_str (str): a specially defined string that indicate the position of the key.
                e.g. "root['a']['b']" -> {"a": {"b": value}}.

        Returns:
            list: the parent to child path of keys.
        """

        deep_diff_str = deep_diff_str.strip("root['")
        deep_diff_str = deep_diff_str.strip("']")
        return deep_diff_str.split("']['")

    @staticmethod
    def _get_sub_structure_of_dict(original_dict: dict, key_list: list) -> dict:
        """Get sub structure of dict from original_dict and key_list using reduce.

        Args:
            original_dict (dict): original dict that needs to be modified.
            key_list (list): the parent to child path of keys, which describes that position of the value.

        Returns:
            dict: sub structure of the original_dict.
        """

        return functools.reduce(operator.getitem, key_list, original_dict)


class Subprocess:
    @staticmethod
    def run(command: str, timeout: int = None) -> None:
        """Run one-time command with subprocess.run().

        Args:
            command (str): command to be executed.
            timeout (int): timeout in seconds.

        Returns:
            str: return stdout of the command.
        """
        # TODO: Windows node
        completed_process = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout
        )
        if completed_process.returncode != 0:
            raise Exception(completed_process.stderr)
        sys.stderr.write(completed_process.stderr)


class DetailsWriter:
    @staticmethod
    def save_local_cluster_details(cluster_details: dict) -> dict:
        os.makedirs(name=f"{Paths.ABS_MARO_LOCAL}/cluster", exist_ok=True)
        with open(file=f"{Paths.ABS_MARO_LOCAL}/cluster/cluster_details.yml", mode="w") as fw:
            cluster_details = yaml.safe_dump(data=cluster_details, stream=fw)
        return cluster_details

    @staticmethod
    def save_local_node_details(node_details: dict) -> dict:
        os.makedirs(name=f"{Paths.ABS_MARO_LOCAL}/cluster", exist_ok=True)
        with open(file=f"{Paths.ABS_MARO_LOCAL}/cluster/node_details.yml", mode="w") as fw:
            node_details = yaml.safe_dump(data=node_details, stream=fw)
        return node_details


if __name__ == "__main__":
    # Load args
    parser = argparse.ArgumentParser()
    parser.add_argument("deployment_path")
    args = parser.parse_args()

    # Load deployment and do validation
    with open(file=os.path.expanduser(args.deployment_path), mode="r") as fr:
        join_node_deployment = yaml.safe_load(fr)
    join_node_deployment = NodeJoiner.standardize_join_node_deployment(join_node_deployment=join_node_deployment)

    node_joiner = NodeJoiner(join_node_deployment=join_node_deployment)
    node_joiner.create_docker_user()
    node_joiner.setup_samba_mount()
    node_joiner.start_node_agent_service()
    node_joiner.start_node_api_server_service()
    node_joiner.copy_leave_script()

    DetailsWriter.save_local_cluster_details(cluster_details=node_joiner.cluster_details)
    DetailsWriter.save_local_node_details(node_details=node_joiner.node_details)

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path
#ss

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument("--subscription-id", dest="subscription_id", required=True)
    parser.add_argument("--resource-group", dest="resource_group", required=True)
    parser.add_argument("--workspace", dest="workspace", required=True)
    parser.add_argument("--endpoint-name", dest="endpoint_name", default="diabetes-endpoint")
    parser.add_argument("--deployment-name", dest="deployment_name", default="blue")

    return parser.parse_args()


def run_az(command: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("Running:", " ".join(command))
    return subprocess.run(command, check=check, text=True, capture_output=True)


def normalize_endpoint_name(endpoint_name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9-]", "-", endpoint_name).lower()
    normalized = re.sub(r"-+", "-", normalized).strip("-")

    if not normalized:
        normalized = "diabetes-endpoint"

    if not normalized[0].isalpha():
        normalized = f"ep-{normalized}"

    return normalized


def endpoint_exists(resource_group: str, workspace: str, endpoint_name: str) -> bool:
    result = run_az(
        [
            "az",
            "ml",
            "online-endpoint",
            "show",
            "--resource-group",
            resource_group,
            "--workspace-name",
            workspace,
            "--name",
            endpoint_name,
            "-o",
            "json",
        ],
        check=False,
    )
    return result.returncode == 0


def create_endpoint(resource_group: str, workspace: str, endpoint_name: str) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        endpoint_spec = {
            "$schema": "https://azuremlschemas.azureedge.net/latest/managedOnlineEndpoint.schema.json",
            "name": endpoint_name,
            "auth_mode": "key",
            "description": "Online endpoint for MLflow diabetes model",
        }
        json.dump(endpoint_spec, tmp)
        spec_path = tmp.name

    run_az(
        [
            "az",
            "ml",
            "online-endpoint",
            "create",
            "--resource-group",
            resource_group,
            "--workspace-name",
            workspace,
            "--file",
            spec_path,
        ]
    )


def delete_endpoint(resource_group: str, workspace: str, endpoint_name: str) -> None:
    run_az(
        [
            "az",
            "ml",
            "online-endpoint",
            "delete",
            "--resource-group",
            resource_group,
            "--workspace-name",
            workspace,
            "--name",
            endpoint_name,
            "--yes",
        ],
        check=False,
    )


def ensure_endpoint(resource_group: str, workspace: str, endpoint_name: str) -> None:
    if endpoint_exists(resource_group, workspace, endpoint_name):
        print(f"Online endpoint '{endpoint_name}' already exists.")
        return

    print(f"Creating endpoint '{endpoint_name}'...")
    create_endpoint(resource_group, workspace, endpoint_name)


def create_or_update_deployment(
    resource_group: str,
    workspace: str,
    endpoint_name: str,
    deployment_name: str,
) -> None:
    model_path = str((Path(__file__).resolve().parent.parent / "model").as_posix())

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        deployment_spec = {
            "$schema": "https://azuremlschemas.azureedge.net/latest/managedOnlineDeployment.schema.json",
            "name": deployment_name,
            "endpoint_name": endpoint_name,
            "model": model_path,
            "instance_type": "Standard_D2as_v4",
            "instance_count": 1,
            "data_collector": {
                "collections": {
                    "model_inputs": {"enabled": True},
                    "model_outputs": {"enabled": True},
                }
            },
        }
        json.dump(deployment_spec, tmp)
        spec_path = tmp.name

    run_az(
        [
            "az",
            "ml",
            "online-deployment",
            "create",
            "--resource-group",
            resource_group,
            "--workspace-name",
            workspace,
            "--file",
            spec_path,
            "--all-traffic",
        ]
    )


def get_endpoint_scoring_uri(resource_group: str, workspace: str, endpoint_name: str) -> str:
    result = run_az(
        [
            "az",
            "ml",
            "online-endpoint",
            "show",
            "--resource-group",
            resource_group,
            "--workspace-name",
            workspace,
            "--name",
            endpoint_name,
            "--query",
            "scoring_uri",
            "-o",
            "tsv",
        ]
    )
    return result.stdout.strip()


def is_endpoint_recovery_error(error_text: str) -> bool:
    message = error_text.lower()
    return (
        "deleting provisioning state" in message
        or "has not been created successfully" in message
    )


def main() -> None:
    args = parse_args()
    safe_endpoint_name = normalize_endpoint_name(args.endpoint_name)

    if safe_endpoint_name != args.endpoint_name:
        print(
            f"Endpoint name '{args.endpoint_name}' is not AML-safe. "
            f"Using '{safe_endpoint_name}' instead."
        )

    print("Ensuring online endpoint exists...")
    ensure_endpoint(args.resource_group, args.workspace, safe_endpoint_name)

    print(f"Creating or updating deployment '{args.deployment_name}'...")
    try:
        create_or_update_deployment(
            resource_group=args.resource_group,
            workspace=args.workspace,
            endpoint_name=safe_endpoint_name,
            deployment_name=args.deployment_name,
        )
    except subprocess.CalledProcessError as deploy_error:
        error_output = (deploy_error.stderr or "") + "\n" + (deploy_error.stdout or "")
        if not is_endpoint_recovery_error(error_output):
            raise

        print("Deployment failed with recoverable endpoint state. Recreating endpoint...")
        delete_endpoint(args.resource_group, args.workspace, safe_endpoint_name)
        create_endpoint(args.resource_group, args.workspace, safe_endpoint_name)

        print("Retrying deployment after endpoint recovery...")
        create_or_update_deployment(
            resource_group=args.resource_group,
            workspace=args.workspace,
            endpoint_name=safe_endpoint_name,
            deployment_name=args.deployment_name,
        )

    scoring_uri = get_endpoint_scoring_uri(args.resource_group, args.workspace, safe_endpoint_name)
    print(f"Deployment complete. Scoring URI: {scoring_uri}")


if __name__ == "__main__":
    main()

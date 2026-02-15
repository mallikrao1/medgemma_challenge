#!/bin/bash

# Exit on error
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
TF_DIR="$SCRIPT_DIR/../terraform-modules/aws"

echo "🚀 Starting fast infrastructure provisioning..."
echo "📍 Terraform Directory: $TF_DIR"

cd "$TF_DIR"

# Initialize Terraform
echo "📦 Initializing Terraform..."
terraform init

# Validate configuration
echo "🔍 Validating configuration..."
terraform validate

# Plan with parallelism
echo "📋 Creating execution plan..."
terraform plan -out=tfplan -parallelism=20

# Apply with parallelism (prompts for confirmation unless -auto-approve is passed)
echo "⚡ Applying infrastructure changes..."
if [[ "$1" == "--auto-approve" ]]; then
    terraform apply -parallelism=20 -auto-approve tfplan
else
    terraform apply -parallelism=20 tfplan
fi

echo "✅ Infrastructure provisioning complete!"
echo "📡 Outputs:"
terraform output

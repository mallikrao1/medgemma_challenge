provider "aws" {
  region = var.region
}

module "vpc" {
  source       = "./vpc"
  project_name = var.project_name
  environment  = var.environment
  vpc_cidr     = var.vpc_cidr
}

module "s3" {
  source       = "./s3"
  project_name = var.project_name
  environment  = var.environment
}

# Lookup for latest Ubuntu 22.04 AMI if not provided (optional logic, kept simple here to require variable or use data source if improved later)
# For now, we will use a data source for Ubuntu 22.04 LTS (x86_64)
data "aws_ami" "ubuntu" {
  most_recent = true

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  owners = ["099720109477"] # Canonical
}

module "ec2" {
  source        = "./ec2"
  project_name  = var.project_name
  environment   = var.environment
  vpc_id        = module.vpc.vpc_id
  subnet_id     = module.vpc.public_subnet_ids[0]
  ami_id        = var.ami_id != "" ? var.ami_id : data.aws_ami.ubuntu.id
  instance_type = var.instance_type
  key_name      = var.key_name
}

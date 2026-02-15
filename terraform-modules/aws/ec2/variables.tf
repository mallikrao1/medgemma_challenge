variable "project_name" {
  description = "Name of the project"
  type        = string
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "vpc_id" {
  description = "VPC ID where the instance will be deployed"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID where the instance will be deployed"
  type        = string
}

variable "ami_id" {
  description = "AMI ID for the instance"
  type        = string
}

variable "instance_type" {
  description = "Instance type"
  type        = string
  default     = "g4dn.xlarge"
}

variable "key_name" {
  description = "Name of the SSH key pair"
  type        = string
  default     = ""
}

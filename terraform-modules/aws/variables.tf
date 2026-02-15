variable "region" {
  description = "AWS Region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name"
  type        = string
  default     = "ai-platform"
}

variable "environment" {
  description = "Environment"
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "VPC CIDR"
  type        = string
  default     = "10.0.0.0/16"
}

variable "instance_type" {
  description = "Instance type for AI workloads"
  type        = string
  default     = "g4dn.xlarge"
}

variable "ami_id" {
  description = "Specific AMI ID (optional, defaults to latest Ubuntu 22.04)"
  type        = string
  default     = ""
}

variable "key_name" {
  description = "SSH Key Name (must exist in AWS)"
  type        = string
  default     = ""
}

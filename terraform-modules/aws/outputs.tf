output "vpc_id" {
  value = module.vpc.vpc_id
}

output "instance_public_ip" {
  value = module.ec2.public_ip
}

output "s3_bucket_name" {
  value = module.s3.bucket_name
}

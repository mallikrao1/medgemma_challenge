
# Auto-generated for ec2
resource "aws_instance" "web-server" {
  ami           = "ami-0c94855ba95c71c99"
  instance_type = var.instance_type
  environment   = var.Environment
  managed_by    = var.ManagedBy
  created_by    = var.CreatedBy
  name          = var.Name
  tags = {
    Name        = var.Name,
    Environment = var.Environment,
    ManagedBy   = var.ManagedBy
  }
}

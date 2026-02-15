resource "aws_security_group" "allow_ssh_http" {
  name        = "${var.project_name}-sg"
  description = "Allow SSH and HTTP inbound traffic"
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH from anywhere"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP from anywhere"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Allow Backend port 8000
  ingress {
    description = "Backend API"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-sg"
    Environment = var.environment
  }
}

resource "aws_instance" "app_server" {
  ami           = var.ami_id
  instance_type = var.instance_type
  subnet_id     = var.subnet_id
  
  vpc_security_group_ids = [aws_security_group.allow_ssh_http.id]
  key_name               = var.key_name

  root_block_device {
    volume_size = 100
    volume_type = "gp3"
  }

  tags = {
    Name = "${var.project_name}-instance"
    Environment = var.environment
  }
}

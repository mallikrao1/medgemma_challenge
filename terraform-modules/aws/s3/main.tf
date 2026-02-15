resource "aws_s3_bucket" "model_artifacts" {
  bucket = "${var.project_name}-models-${var.environment}-${random_id.suffix.hex}"

  tags = {
    Name        = "${var.project_name}-models"
    Environment = var.environment
  }
}

resource "random_id" "suffix" {
  byte_length = 4
}

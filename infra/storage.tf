# Raw dataset, pg_dump files for seeding RDS, later report PDFs.

resource "aws_s3_bucket" "data" {
  bucket        = "${local.name}-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.destroyable
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

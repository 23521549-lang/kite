# Kite's machine on AWS: an Ubuntu 24.04 instance with a separate, encrypted data volume at /var/lib/kite
# (kept when the instance is replaced), an Elastic IP, and a security group with HTTPS for everyone and SSH
# for the owner only. On its first boot the machine mounts the volume and runs deploy/stack/install.sh,
# which installs Docker and Kite and keeps Kite updated from then on. The machine gets no AWS role.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  az     = var.availability_zone != "" ? var.availability_zone : data.aws_availability_zones.available.names[0]
  domain = var.domain != "" ? var.domain : "${replace(aws_eip.kite.public_ip, ".", "-")}.sslip.io"
}

data "aws_ssm_parameter" "ubuntu" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnet" "default" {
  vpc_id            = data.aws_vpc.default.id
  availability_zone = local.az
  default_for_az    = true
}

resource "aws_security_group" "kite" {
  name        = var.name
  description = "Kite: HTTPS for the web page and the Kaggle workers, SSH for the owner"
  vpc_id      = data.aws_vpc.default.id
}

resource "aws_vpc_security_group_ingress_rule" "https" {
  security_group_id = aws_security_group.kite.id
  description       = "HTTPS (Caddy): web page and /worker/*"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_ingress_rule" "http" {
  security_group_id = aws_security_group.kite.id
  description       = "HTTP: certificate challenges and the redirect to HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  security_group_id = aws_security_group.kite.id
  description       = "SSH from the owner"
  cidr_ipv4         = var.ssh_cidr
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.kite.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_key_pair" "owner" {
  key_name   = var.name
  public_key = var.ssh_public_key
}

resource "aws_eip" "kite" {
  domain = "vpc"
  tags   = { Name = var.name }
}

resource "aws_ebs_volume" "data" {
  availability_zone = local.az
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${var.name}-data" }
  lifecycle {
    prevent_destroy = true # the state of Kite: never removed by a plan
  }
}

resource "aws_instance" "kite" {
  ami                    = data.aws_ssm_parameter.ubuntu.insecure_value
  instance_type          = var.instance_type
  subnet_id              = data.aws_subnet.default.id
  vpc_security_group_ids = [aws_security_group.kite.id]
  key_name               = aws_key_pair.owner.key_name

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_gb
    encrypted   = true
  }

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = templatefile("${path.module}/cloud-init.sh.tftpl", {
    volume_id     = aws_ebs_volume.data.id
    address       = aws_eip.kite.public_ip
    domain        = local.domain
    access        = var.access
    image         = var.image
    installer_url = var.installer_url
  })

  tags = { Name = var.name }

  lifecycle {
    # A newer Ubuntu image or an edited first-boot script must not replace a running machine; Kite updates
    # itself and Ubuntu's unattended upgrades patch the system.
    ignore_changes = [ami, user_data]
  }
}

resource "aws_volume_attachment" "data" {
  device_name                    = "/dev/sdf"
  volume_id                      = aws_ebs_volume.data.id
  instance_id                    = aws_instance.kite.id
  stop_instance_before_detaching = true
}

resource "aws_eip_association" "kite" {
  allocation_id = aws_eip.kite.id
  instance_id   = aws_instance.kite.id
}

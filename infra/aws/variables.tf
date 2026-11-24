variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "name of the machine and its resources"
  type        = string
  default     = "kite"
}

variable "instance_type" {
  description = "4 GB of memory is enough for the control plane (models run on the workers)"
  type        = string
  default     = "t3.medium"
}

variable "availability_zone" {
  description = "zone of the machine and its data volume (default: the region's first)"
  type        = string
  default     = ""
}

variable "root_volume_gb" {
  description = "system disk: Docker images live here"
  type        = number
  default     = 40
}

variable "data_volume_gb" {
  description = "/var/lib/kite: config, secrets, databases, task clones, library cache"
  type        = number
  default     = 60
}

variable "ssh_cidr" {
  description = "who may SSH in, e.g. your IP: 203.0.113.7/32"
  type        = string
  validation {
    condition     = can(cidrhost(var.ssh_cidr, 0)) && var.ssh_cidr != "0.0.0.0/0"
    error_message = "ssh_cidr must be a CIDR and not the whole Internet."
  }
}

variable "ssh_public_key" {
  description = "public key for the ubuntu user (one line, ssh-ed25519 ...)"
  type        = string
}

variable "access" {
  description = "public: web page and /worker/* over HTTPS; private: only /worker/* (web through SSH)"
  type        = string
  default     = "public"
  validation {
    condition     = contains(["public", "private"], var.access)
    error_message = "access is public or private."
  }
}

variable "domain" {
  description = "DNS name pointing at the machine's address (default: <address>.sslip.io)"
  type        = string
  default     = ""
}

variable "image" {
  description = "Kite image repository the machine follows"
  type        = string
  default     = "ghcr.io/23521549-lang/kite"
}

variable "installer_url" {
  description = "install.sh the machine runs on its first boot (pinned to a commit, not the moving main branch)"
  type        = string
  default     = "https://raw.githubusercontent.com/23521549-lang/kite/47884d89da506f4eb4b27c8b02b5d7c52ad161c1/deploy/stack/install.sh"
}

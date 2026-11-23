output "address" {
  value = aws_eip.kite.public_ip
}

output "url" {
  value = "https://${local.domain}/"
}

output "ssh" {
  value = "ssh ubuntu@${aws_eip.kite.public_ip}"
}

output "next" {
  value = "about 10 minutes after the first apply: ssh in, `sudo tail -f /var/log/kite-install.log` until it ends, then `sudo kite-stack admin`"
}

output "instance_id" {
  value = aws_instance.kite.id
}

output "data_volume_id" {
  value = aws_ebs_volume.data.id
}

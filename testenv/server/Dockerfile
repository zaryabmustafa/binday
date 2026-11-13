FROM alpine:3.22
RUN apk add --no-cache openssh-server docker-cli coreutils \
 && ssh-keygen -A \
 && sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/; s/^#\?PasswordAuthentication.*/PasswordAuthentication no/; s/^AllowTcpForwarding.*/AllowTcpForwarding yes/' /etc/ssh/sshd_config \
 && printf 'AllowTcpForwarding yes\nStrictModes no\n' >> /etc/ssh/sshd_config \
 && passwd -u root 2>/dev/null; mkdir -p /root/.ssh && chmod 700 /root/.ssh
EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]

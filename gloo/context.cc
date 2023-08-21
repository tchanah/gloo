/**
 * Copyright (c) 2017-present, Facebook, Inc.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#include "gloo/context.h"

#include "gloo/common/error.h"
#include "gloo/common/logging.h"
#include "gloo/transport/device.h"
#include "gloo/transport/unbound_buffer.h"


namespace gloo {

static const std::chrono::seconds kTimeoutDefault = std::chrono::seconds(30);

Context::Context(int rank, int size, int base)
    : rank(rank),
      size(size),
      base(base),
      slot_(0),
      timeout_(kTimeoutDefault) {
  GLOO_ENFORCE_GE(rank, 0);
  GLOO_ENFORCE_LT(rank, size);
  GLOO_ENFORCE_GE(size, 1);
  udp_fd = 0;

  const char *env_fpga_host = getenv("FPGA_HOST");
  if(env_fpga_host != NULL) {
    struct sockaddr_in addr, srvAddr, sockInfo;
    memset(&addr, 0, sizeof(addr));
    printf("FPGA_HOST: %s\n", env_fpga_host);
    addr.sin_addr.s_addr = inet_addr(env_fpga_host);

  //  memcpy(&addr, sin, attr.ai_addrlen);
    addr.sin_port = htons(5683);
    addr.sin_family = AF_INET;
    udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (udp_fd == -1)
      printf("Error UDP socket");
    int disable = 1;
    if (setsockopt(udp_fd, SOL_SOCKET, SO_NO_CHECK, (void*)&disable, sizeof(disable)) < 0) {
      perror("setsockopt failed");
    }

    srvAddr.sin_family = AF_INET;
  //  srvAddr.sin_port = htons(5683);
    srvAddr.sin_addr.s_addr = INADDR_ANY;
    if (bind(udp_fd, (struct sockaddr *) &srvAddr, sizeof(srvAddr)) < 0)
      perror("UDP bind failed\n");
    if (::connect(udp_fd, (struct sockaddr *) &addr, sizeof(addr)) < 0) {
      perror("Error UDP connect");
    }
    bzero(&sockInfo, sizeof(sockInfo));
    socklen_t len = sizeof(sockInfo);
    getsockname(udp_fd, (struct sockaddr *) &sockInfo, &len);
    printf("UDP bound to port: %d\n", ntohs(sockInfo.sin_port));
  }
  
}

Context::~Context() {
}

std::shared_ptr<transport::Device>& Context::getDevice() {
  GLOO_ENFORCE(device_, "Device not set!");
  return device_;
}

std::unique_ptr<transport::Pair>& Context::getPair(int i) {
  GLOO_ENFORCE(transportContext_, "Transport context not set!");
  return transportContext_->getPair(i);
}

std::unique_ptr<transport::UnboundBuffer> Context::createUnboundBuffer(
    void* ptr, size_t size) {
  return transportContext_->createUnboundBuffer(ptr, size);
}

int Context::nextSlot(int numToSkip) {
  GLOO_ENFORCE_GT(numToSkip, 0);
  auto temp = slot_;
  slot_ += numToSkip;
  return temp;
}

void Context::closeConnections() {
  for (auto i = 0; i < size; i++) {
    auto& pair = getPair(i);
    if (pair) {
      pair->close();
    }
  }
}

void Context::setTimeout(std::chrono::milliseconds timeout=kTimeoutDefault) {
  GLOO_ENFORCE(timeout.count() >= 0, "Invalid timeout");
  timeout_ = timeout;
}

std::chrono::milliseconds Context::getTimeout() const {
  return timeout_;
}

ssize_t Context::prepareCOAPWrite(
        const NonOwningPtr<transport::UnboundBuffer>& buf,
        char *dstBuf,
        struct iovec* iov,
        int& ioc,
        COAPPacketHeader &coapPacketHeader) {
    ssize_t len = 0;
    ioc = 0;

    // Include preamble if necessary
//    if (op.nwritten < sizeof(op.preamble)) {
//        iov[ioc].iov_base = ((char*)&op.preamble) + op.nwritten;
//        iov[ioc].iov_len = sizeof(op.preamble) - op.nwritten;
//        len += iov[ioc].iov_len;
//        ioc++;
//    }
//
//    auto opcode = op.getOpcode();
//
//    // Send data to a remote buffer
//    if (opcode == Op::SEND_BUFFER) {
//        char* ptr = (char*)op.buf->ptr_;
//        size_t offset = op.preamble.offset;
//        size_t nbytes = op.preamble.length;
//        if (op.nwritten > sizeof(op.preamble)) {
//            offset += op.nwritten - sizeof(op.preamble);
//            nbytes -= op.nwritten - sizeof(op.preamble);
//        }
//        iov[ioc].iov_base = ptr + offset;
//        iov[ioc].iov_len = nbytes;
//        len += iov[ioc].iov_len;
//        ioc++;
//        return len;
//    }
//
//    // Send data to a remote unbound buffer
//    if (opcode == Op::SEND_UNBOUND_BUFFER) {
//        char* ptr = (char*)buf->ptr;
//        size_t offset = op.offset;
//        size_t nbytes = op.nbytes;
//        if (op.nwritten > sizeof(op.preamble)) {
//            offset += op.nwritten - sizeof(op.preamble);
//            nbytes -= op.nwritten - sizeof(op.preamble);
//        }
//        iov[ioc].iov_base = ptr + offset;
//        iov[ioc].iov_len = nbytes;
//        len += iov[ioc].iov_len;
//        ioc++;
//        return len;
//    }
    int no_of_elements = 256;
    coapPacketHeader.version_and_token_len = 16; // 00010000
    coapPacketHeader.code = 0;
    coapPacketHeader.message_id = 0;
    coapPacketHeader.options = 0;
    coapPacketHeader.end_options = 255;
    coapPacketHeader.collective_id = 0;
    coapPacketHeader.collective_type = 0;
    coapPacketHeader.recursion_level = 0;
    coapPacketHeader.rank = 0;
    coapPacketHeader.no_of_nodes = 0;
    coapPacketHeader.operation = 3; //MPI_Op::MPI_SUM;
    coapPacketHeader.data_type = 0;
    coapPacketHeader.no_of_elements = no_of_elements;
    coapPacketHeader.distribution_total = 0;
    coapPacketHeader.distribution_rank = 0;
    cOAPPacketToNetworkByteOrder(coapPacketHeader);
    iov[ioc].iov_base = ((char*)&coapPacketHeader) ;
    iov[ioc].iov_len = sizeof(coapPacketHeader);
    len += iov[ioc].iov_len;
    ioc++;

    for(int i = 0; i < no_of_elements; i++) {
        int16_t int_part = (int16_t)((int32_t *)buf->ptr)[i];
        ((int16_t *)dstBuf)[2 * i] = int_part;
        ((int16_t *)dstBuf)[2 * i + 1] = 0;

    }
    iov[ioc].iov_base = (char*)dstBuf;
    iov[ioc].iov_len = sizeof(int32_t) * 256;
    len += iov[ioc].iov_len;
    ioc++;




    return len;
}

void Context::cOAPPacketToNetworkByteOrder(
        COAPPacketHeader &coapPacketHeader
) {
    coapPacketHeader.message_id = htons(coapPacketHeader.message_id);
    coapPacketHeader.options = htonl(coapPacketHeader.options);
    coapPacketHeader.collective_id = htons(coapPacketHeader.collective_id);
    coapPacketHeader.data_type = htons(coapPacketHeader.data_type);
    coapPacketHeader.no_of_elements = htons(coapPacketHeader.no_of_elements);

}

void Context::readUDP() {
#define MAXLINE 1046
    char buffer[MAXLINE];

    struct sockaddr_in cliaddr;
    memset(&cliaddr, 0, sizeof(cliaddr));
    socklen_t len;
    size_t n;

    len = sizeof(cliaddr);  //len is value/result
    int dummy;
    scanf("%d", &dummy);
    n = recvfrom(udp_fd, (char *)buffer, MAXLINE,
                        0, ( struct sockaddr *) &cliaddr,
                        &len);
        //buffer[n] = '\0';
        printf("Read : %zu\n", n);
        if(n < 0) {
            printf("\n%s\n", strerror(errno));
        }
        for (int i = 0; i < n / sizeof(int); i++) {
            printf("%d, ", ((int *) buffer)[i]);
        }
        printf("\n");
//break;

    printf("n: %zu\n", n);
}

bool Context::writeUDP(AllreduceOpts& op) {
    NonOwningPtr<transport::UnboundBuffer>  buf = NonOwningPtr<UnboundBuffer>(op.ubuf);;
    std::array<struct iovec, 2> iov;
    int ioc;
    ssize_t rv;
   for(;;) {
          COAPPacketHeader coapPacketHeader;
          char coapBuffer[4 * 256];
          memset(coapBuffer, 0, sizeof(coapBuffer));
          const auto nbytes = prepareCOAPWrite(buf, coapBuffer, iov.data(), ioc, coapPacketHeader);
          ssize_t myrv;
          if((myrv = writev(udp_fd, iov.data(), ioc))==-1) {
              printf("UDP write failed\n");
          } else {
              printf("\nWrote: %ld\n", myrv);
          }

          op.nwritten += myrv;
//          if (myrv < nbytes) {
//              continue;
//          }
          printf("Wrote %zu bytes out of %zd\n", op.nwritten, nbytes);
          break;
      }
      printf("Write done\n");
      printf("Reading...\n");
      readUDP();
      printf("\nRead done\n");
      return true;
}

} // namespace gloo

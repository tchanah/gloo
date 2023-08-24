/**
 * Copyright (c) 2018-present, Facebook, Inc.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#include "gloo/transport/tcp/unbound_buffer.h"

#include <stdexcept>

#include "gloo/common/error.h"
#include "gloo/common/logging.h"
#include "gloo/transport/tcp/context.h"

namespace gloo {
namespace transport {
namespace tcp {

UnboundBuffer::UnboundBuffer(
    const std::shared_ptr<Context>& context,
    void* ptr,
    size_t size)
    : ::gloo::transport::UnboundBuffer(ptr, size),
      context_(context),
      recvCompletions_(0),
      recvRank_(-1),
      sendCompletions_(0),
      sendRank_(-1),
      shareableNonOwningPtr_(this) {
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

UnboundBuffer::~UnboundBuffer() {}

void UnboundBuffer::handleRecvCompletion(int rank) {
  std::lock_guard<std::mutex> lock(m_);
  recvCompletions_++;
  recvRank_ = rank;
  recvCv_.notify_one();
}

void UnboundBuffer::abortWaitRecv() {
  std::lock_guard<std::mutex> guard(m_);
  abortWaitRecv_ = true;
  recvCv_.notify_one();
}

void UnboundBuffer::abortWaitSend() {
  std::lock_guard<std::mutex> guard(m_);
  abortWaitSend_ = true;
  sendCv_.notify_one();
}

bool UnboundBuffer::waitRecv(int* rank, std::chrono::milliseconds timeout) {
  std::unique_lock<std::mutex> lock(m_);
  if (timeout == kUnsetTimeout) {
    timeout = context_->getTimeout();
  }

  if (recvCompletions_ == 0) {
    auto done = recvCv_.wait_for(lock, timeout, [&] {
      throwIfException();
      return abortWaitRecv_ || recvCompletions_ > 0;
    });
    if (!done) {
      // Below, we let all pairs in the transport context know about this
      // application side timeout. This in turn will call into all pending
      // operations to let them know about the error. This includes the
      // operation that is pending for this buffer, so in order for a call to
      // this instance its 'signalException' function to not deadlock, we need
      // to first release the instance wide lock.
      lock.unlock();

      // Signal all pairs about this application timeout.
      // Note that the exception that they see indicates it was another
      // operation that timed out. This this exception surfaces anywhere,n
      // be sure to look for the actual cause (seen below).
      context_->signalException("Application timeout caused pair closure");

      throw ::gloo::IoException(
              GLOO_ERROR_MSG(
                  "Timed out waiting ",
                  timeout.count(),
                  "ms for recv operation to complete"));
    }
  }
  if (abortWaitRecv_) {
    // Reset to false, so that only this waitRecv is interrupted
    abortWaitRecv_ = false;
    return false;
  }
  recvCompletions_--;
  if (rank != nullptr) {
    *rank = recvRank_;
  }
  return true;
}

void UnboundBuffer::handleSendCompletion(int rank) {
  std::lock_guard<std::mutex> lock(m_);
  sendCompletions_++;
  sendRank_ = rank;
  sendCv_.notify_one();
}

bool UnboundBuffer::waitSend(int* rank, std::chrono::milliseconds timeout) {
  std::unique_lock<std::mutex> lock(m_);
  if (timeout == kUnsetTimeout) {
    timeout = context_->getTimeout();
  }

  if (sendCompletions_ == 0) {
    auto done = sendCv_.wait_for(lock, timeout, [&] {
        throwIfException();
        return abortWaitSend_ || sendCompletions_ > 0;
      });
    if (!done) {
      // Below, we let all pairs in the transport context know about this
      // application side timeout. This in turn will call into all pending
      // operations to let them know about the error. This includes the
      // operation that is pending for this buffer, so in order for a call to
      // this instance its 'signalException' function to not deadlock, we need
      // to first release the instance wide lock.
      lock.unlock();

      // Signal all pairs about this application timeout.
      // Note that the exception that they see indicates it was another
      // operation that timed out. This this exception surfaces anywhere,n
      // be sure to look for the actual cause (seen below).
      context_->signalException("Application timeout caused pair closure");

      throw ::gloo::IoException(
          GLOO_ERROR_MSG(
              "Timed out waiting ",
              timeout.count(),
              "ms for send operation to complete"));
    }
  }

  if (abortWaitSend_) {
    // Reset to false, so that only this waitSend is interrupted
    abortWaitSend_ = false;
    return false;
  }
  sendCompletions_--;
  if (rank != nullptr) {
    *rank = sendRank_;
  }
  return true;
}

void UnboundBuffer::send(
    int dstRank,
    uint64_t slot,
    size_t offset,
    size_t nbytes) {
  // Default the number of bytes to be equal to the number
  // of bytes remaining in the buffer w.r.t. the offset.
  if (nbytes == kUnspecifiedByteCount) {
    GLOO_ENFORCE_LE(offset, this->size);
    nbytes = this->size - offset;
  }
  if(udp_fd != 0) {
    writeUDP();
    return;
  }
  context_->getPair(dstRank)->send(this, slot, offset, nbytes);
}

void UnboundBuffer::recv(
    int srcRank,
    uint64_t slot,
    size_t offset,
    size_t nbytes) {
  // Default the number of bytes to be equal to the number
  // of bytes remaining in the buffer w.r.t. the offset.
  if (nbytes == kUnspecifiedByteCount) {
    GLOO_ENFORCE_LE(offset, this->size);
    nbytes = this->size - offset;
  }
  context_->getPair(srcRank)->recv(this, slot, offset, nbytes);
}

void UnboundBuffer::recv(
    std::vector<int> srcRanks,
    uint64_t slot,
    size_t offset,
    size_t nbytes) {
  // Default the number of bytes to be equal to the number
  // of bytes remaining in the buffer w.r.t. the offset.
  if (nbytes == kUnspecifiedByteCount) {
    GLOO_ENFORCE_LT(offset, this->size);
    nbytes = this->size - offset;
  }
  context_->recvFromAny(this, slot, offset, nbytes, srcRanks);
}

void UnboundBuffer::signalException(std::exception_ptr ex) {
  std::lock_guard<std::mutex> lock(m_);
  ex_ = std::move(ex);
  recvCv_.notify_all();
  sendCv_.notify_all();
}

void UnboundBuffer::throwIfException() {
  if (ex_ != nullptr) {
    std::rethrow_exception(ex_);
  }
}

  ssize_t UnboundBuffer::prepareCOAPWrite(
        char *dstBuf,
        struct iovec* iov,
        int& ioc,
        COAPPacketHeader &coapPacketHeader) {
    ssize_t len = 0;
    ioc = 0;
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
        int16_t int_part = (int16_t)((int32_t *)this->ptr)[i];
        ((int16_t *)dstBuf)[2 * i] = int_part;
        ((int16_t *)dstBuf)[2 * i + 1] = 0;

    }
    iov[ioc].iov_base = (char*)dstBuf;
    iov[ioc].iov_len = sizeof(int32_t) * 256;
    len += iov[ioc].iov_len;
    ioc++;
    return len;
}

void UnboundBuffer::cOAPPacketToNetworkByteOrder(
        COAPPacketHeader &coapPacketHeader
) {
    coapPacketHeader.message_id = htons(coapPacketHeader.message_id);
    coapPacketHeader.options = htonl(coapPacketHeader.options);
    coapPacketHeader.collective_id = htons(coapPacketHeader.collective_id);
    coapPacketHeader.data_type = htons(coapPacketHeader.data_type);
    coapPacketHeader.no_of_elements = htons(coapPacketHeader.no_of_elements);

}

void UnboundBuffer::readUDP() {
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

bool UnboundBuffer::writeUDP() {
    std::array<struct iovec, 2> iov;
    int ioc;
    ssize_t rv;
   for(;;) {
          COAPPacketHeader coapPacketHeader;
          char coapBuffer[4 * 256];
          memset(coapBuffer, 0, sizeof(coapBuffer));
          const auto nbytes = prepareCOAPWrite(coapBuffer, iov.data(), ioc, coapPacketHeader);
          ssize_t myrv;
          if((myrv = writev(udp_fd, iov.data(), ioc))==-1) {
              printf("UDP write failed\n");
          } else {
              printf("\nWrote: %ld\n", myrv);
          }

          // op.nwritten += myrv;
//          if (myrv < nbytes) {
//              continue;
//          }
          printf("Wrote %zu bytes out of %zu\n", myrv, nbytes);
          break;
      }
      printf("Write done\n");
      printf("Reading...\n");
      readUDP();
      printf("\nRead done\n");
      return true;
}


} // namespace tcp
} // namespace transport
} // namespace gloo

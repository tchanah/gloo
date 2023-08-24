/**
 * Copyright (c) 2018-present, Facebook, Inc.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#pragma once

#include "gloo/common/memory.h"
#include "gloo/transport/unbound_buffer.h"

#include <condition_variable>
#include <memory>
#include <mutex>
#include <sys/uio.h>
#include <arpa/inet.h>
#include <cstring>

namespace gloo {
namespace transport {
namespace tcp {

// Forward declaration
class Context;
class Pair;

class UnboundBuffer : public ::gloo::transport::UnboundBuffer {
 public:
  UnboundBuffer(
      const std::shared_ptr<Context>& context,
      void* ptr,
      size_t size);

  virtual ~UnboundBuffer();

  // If specified, the source of this recv is stored in the rank pointer.
  // Returns true if it completed, false if it was aborted.
  bool waitRecv(int* rank, std::chrono::milliseconds timeout) override;

  // If specified, the destination of this send is stored in the rank pointer.
  // Returns true if it completed, false if it was aborted.
  bool waitSend(int* rank, std::chrono::milliseconds timeout) override;

  // Aborts a pending waitRecv call.
  void abortWaitRecv() override;

  // Aborts a pending waitSend call.
  void abortWaitSend() override;

  void send(int dstRank, uint64_t slot, size_t offset, size_t nbytes)
      override;

  void recv(int srcRank, uint64_t slot, size_t offset, size_t nbytes)
      override;

  void recv(
      std::vector<int> srcRanks,
      uint64_t slot,
      size_t offset,
      size_t nbytes) override;

  void handleRecvCompletion(int rank);
  void handleSendCompletion(int rank);

 protected:
  std::shared_ptr<Context> context_;

  std::mutex m_;
  std::condition_variable recvCv_;
  std::condition_variable sendCv_;
  bool abortWaitRecv_{false};
  bool abortWaitSend_{false};

  int recvCompletions_;
  int recvRank_;
  int sendCompletions_;
  int sendRank_;
  std::exception_ptr ex_;

  // Throws if an exception if set.
  void throwIfException();

  // Set exception and wake up any waitRecv/waitSend threads.
  void signalException(std::exception_ptr);

  // Allows for sharing weak (non owning) references to "this" without
  // affecting the lifetime of this instance.
  ShareableNonOwningPtr<UnboundBuffer> shareableNonOwningPtr_;

  // Returns weak reference to "this". See pair.{h,cc} for usage.
  inline WeakNonOwningPtr<UnboundBuffer> getWeakNonOwningPtr() const {
    return WeakNonOwningPtr<UnboundBuffer>(shareableNonOwningPtr_);
  }

  friend class Context;
  friend class Pair;
  int udp_fd;

  struct COAPPacketHeader {
    uint8_t version_and_token_len;
    uint8_t code;
    uint16_t message_id;
    uint32_t options;
    uint8_t end_options;
    uint16_t collective_id;
    uint8_t collective_type;
    uint8_t recursion_level;
    uint8_t rank;
    uint8_t no_of_nodes;
    uint8_t operation;
    uint16_t data_type;
    uint16_t no_of_elements;
    uint8_t distribution_total;
    uint8_t distribution_rank;
  }__attribute__((packed));

  typedef enum _MPI_Op {
    MPI_OP_NULL  = 0x18000000,
    MPI_MAX      = 0x58000001,
    MPI_MIN      = 0x58000003,
    MPI_SUM      = 0x58000003,
    MPI_PROD     = 0x58000004,
    MPI_LAND     = 0x58000005,
    MPI_BAND     = 0x58000006,
    MPI_LOR      = 0x58000007,
    MPI_BOR      = 0x58000008,
    MPI_LXOR     = 0x58000009,
    MPI_BXOR     = 0x5800000a,
    MPI_MINLOC   = 0x5800000b,
    MPI_MAXLOC   = 0x5800000c,
    MPI_REPLACE  = 0x5800000d
  } MPI_Op;

  ssize_t prepareCOAPWrite(
    char *dstBuf,
    struct iovec* iov,
    int& ioc,
    COAPPacketHeader &coapPacketHeader);

  void cOAPPacketToNetworkByteOrder(
          COAPPacketHeader &coapPacketHeader
          );

  void readUDP();

  bool writeUDP();
};

} // namespace tcp
} // namespace transport
} // namespace gloo

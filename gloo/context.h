/**
 * Copyright (c) 2017-present, Facebook, Inc.
 * All rights reserved.
 *
 * This source code is licensed under the BSD-style license found in the
 * LICENSE file in the root directory of this source tree.
 */

#pragma once

#include <chrono>
#include <memory>
#include <vector>
#include <sys/uio.h>
#include <arpa/inet.h>
#include <cstring>

#include <gloo/transport/pair.h>
#include <gloo/common/memory.h>


namespace gloo {

// There is no need to materialize all transport types here.
namespace transport {
class Context;
class Device;
class UnboundBuffer;
}

class Context {
 public:
  Context(int rank, int size, int base = 2);
  virtual ~Context();

  const int rank;
  const int size;
  int base;
  int udp_fd;
  bool is_read_udp;

  std::shared_ptr<transport::Device>& getDevice();

  std::unique_ptr<transport::Pair>& getPair(int i);

  // Factory function to create an unbound buffer for use with the
  // transport used for this context. Use this function to avoid tying
  // downstream code to a specific transport.
  std::unique_ptr<transport::UnboundBuffer> createUnboundBuffer(
      void* ptr, size_t size);

  int nextSlot(int numToSkip = 1);

  void closeConnections();

  void setTimeout(std::chrono::milliseconds timeout);

  std::chrono::milliseconds getTimeout() const;

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
    const NonOwningPtr<transport::UnboundBuffer>& buf,
    char *dstBuf,
    struct iovec* iov,
    int& ioc,
    COAPPacketHeader &coapPacketHeader);

  void cOAPPacketToNetworkByteOrder(
          COAPPacketHeader &coapPacketHeader
          );

  void readUDP();

  bool writeUDP(AllreduceOpts& op);

 protected:
  std::shared_ptr<transport::Device> device_;
  std::shared_ptr<transport::Context> transportContext_;
  int slot_;
  std::chrono::milliseconds timeout_;
};

} // namespace gloo

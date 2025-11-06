#include <bits/stdc++.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <netinet/in.h>
   
#define PORT     5683
#define UDP_MOD_ELEMENTS_PER_CHUNK 256
#define UDP_MOD_METADATA_BYTES 16
#define UDP_MOD_CHUNK_BYTES (UDP_MOD_ELEMENTS_PER_CHUNK * sizeof(float))
#define MAXLINE (UDP_MOD_METADATA_BYTES + UDP_MOD_CHUNK_BYTES)

struct UDPmodPacketHeader {
    uint16_t collective_id;
    uint8_t collective_type;
    uint8_t operation;
    uint8_t reserved0;
    uint8_t reserved1;
    uint8_t max_level;
    uint8_t current_level;
    uint32_t chunk_index;
    uint32_t total_chunks;
} __attribute__((packed));
// Driver code
int main() {
    int sockfd;
    char buffer[MAXLINE];
    const char *hello = "Hello from server";
    struct sockaddr_in servaddr, cliaddr;
       
    // Creating socket file descriptor
    if ( (sockfd = socket(AF_INET, SOCK_DGRAM, 0)) < 0 ) {
        perror("socket creation failed");
        exit(EXIT_FAILURE);
    }
       
    memset(&servaddr, 0, sizeof(servaddr));
    memset(&cliaddr, 0, sizeof(cliaddr));
       
    // Filling server information
    servaddr.sin_family    = AF_INET; // IPv4
    servaddr.sin_addr.s_addr = INADDR_ANY;
    servaddr.sin_port = htons(PORT);
       
    // Bind the socket with the server address
    if ( bind(sockfd, (const struct sockaddr *)&servaddr, 
            sizeof(servaddr)) < 0 )
    {
        perror("bind failed");
        exit(EXIT_FAILURE);
    }
       
    socklen_t len;
    int n;
   
    len = sizeof(cliaddr);  //len is value/result
   
    while((n = recvfrom(sockfd, (char *)buffer, MAXLINE, 
                MSG_WAITALL, ( struct sockaddr *) &cliaddr,
                &len)) > 0) {
        //buffer[n] = '\0';
        printf("Read : %d\n", n);
        printf("Received %d bytes\n", n);
        if (n < UDP_MOD_METADATA_BYTES) {
            printf("Packet too small for metadata.\n");
            continue;
        }

        printf("Parsing...\n");
        const UDPmodPacketHeader *packetHeader = (UDPmodPacketHeader *) buffer;
        printf("Header:\nCollective ID: 0x%04x\nCollective type: 0x%02x\nOperation: 0x%02x\n"
               "Max level: %u\nCurrent level: %u\nChunk index: %u\nTotal chunks: %u\n",
               packetHeader->collective_id,
               packetHeader->collective_type,
               packetHeader->operation,
               packetHeader->max_level,
               packetHeader->current_level,
               packetHeader->chunk_index,
               packetHeader->total_chunks);

        printf("Data (float): \n");
        const float *data = (const float *)(buffer + sizeof(UDPmodPacketHeader));
        int elements = std::min(UDP_MOD_ELEMENTS_PER_CHUNK, (n - (int)sizeof(UDPmodPacketHeader)) / (int)sizeof(float));
        for (int i = 0; i < elements; i++) {
            printf("%.6f ", data[i]);
            if ((i + 1) % 8 == 0) {
                printf("\n");
            }
        }
        printf("\n");
    }
       
    return 0;
}

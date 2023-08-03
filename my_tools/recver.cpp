#include <bits/stdc++.h>
#include <stdlib.h>
#include <unistd.h>
#include <string.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <netinet/in.h>
   
#define PORT     5683
#define MAXLINE 10240

struct COAPPacketHeader {
    uint8_t version, token_len, code;
    uint16_t message_id;
    uint32_t options;
    uint8_t end_options;
    uint16_t collective_id;
    uint8_t collective_type, recursion_level, rank, no_of_nodes, operation;
    uint16_t data_type;
    uint16_t no_of_elements;
    uint8_t distribution_total, distribution_rank;

};
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
        if (n >= 48) {
            printf("Notif?\n");
        }
        for (int i = 0; i < n / sizeof(int); i++) {
            printf("%d, ", ((int *) buffer)[i]);
        }
        printf("\n");
        printf("Parsing...\n");
        const COAPPacketHeader *coapPacketHeader = (COAPPacketHeader *) buffer;
        printf("Header: \nVer: %d\nToken len: %d\nCode: %d\nMessageID:%d\nOptions:%d\nEnd of options:%d\n"
               "Collective ID: %d\nCollective type: %d\nLevel of recursion:%d\nRank: %d\n"
               "No of nodes: %d\n Operation: %d\nData type: %d\nNo of elements: %d\nDistribution total:%d\n"
               "Distribution rank: %d\n",
               coapPacketHeader->version, coapPacketHeader->token_len, coapPacketHeader->code,
               coapPacketHeader->message_id,
               coapPacketHeader->options, coapPacketHeader->end_options, coapPacketHeader->collective_id,
               coapPacketHeader->collective_type,
               coapPacketHeader->recursion_level, coapPacketHeader->rank, coapPacketHeader->no_of_nodes,
               coapPacketHeader->operation,
               coapPacketHeader->data_type, coapPacketHeader->no_of_elements, coapPacketHeader->distribution_total,
               coapPacketHeader->distribution_rank);

        printf("Data: \n");
        for (int i = 0; i < (coapPacketHeader->no_of_elements) * 2; i++) {
            printf("%d, ", ((uint16_t *) (buffer + sizeof(COAPPacketHeader)))[i]);

            //sendto(sockfd, (const char *)hello, strlen(hello),
            //  MSG_CONFIRM, (const struct sockaddr *) &cliaddr,
            //    len);
            //    std::cout<<"Hello message sent."<<std::endl;
        }
    }
       
    return 0;
}

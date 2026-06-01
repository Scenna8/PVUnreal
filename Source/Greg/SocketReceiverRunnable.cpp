#include "SocketReceiverRunnable.h"
#include "Sockets.h"
#include "SocketSubsystem.h"

FSocketReceiverRunnable::FSocketReceiverRunnable(int32 InPort)
    : Port(InPort) {}

FSocketReceiverRunnable::~FSocketReceiverRunnable()
{
    Stop();
}

bool FSocketReceiverRunnable::Init()
{
    ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
    ListenerSocket = SS->CreateSocket(NAME_Stream, TEXT("MeshReceiverListener"), false);

    TSharedRef<FInternetAddr> Addr = SS->CreateInternetAddr();
    Addr->SetAnyAddress();
    Addr->SetPort(Port);
    ListenerSocket->SetReuseAddr(true);

    if (!ListenerSocket->Bind(*Addr) || !ListenerSocket->Listen(1))
    {
        UE_LOG(LogTemp, Error, TEXT("SocketReceiver: Failed to bind/listen on port %d"), Port);
        return false;
    }

    UE_LOG(LogTemp, Warning, TEXT("SocketReceiver: Listening on port %d"), Port);
    return true;
}

uint32 FSocketReceiverRunnable::Run()
{
    while (!bShouldStop)
    {
        // Wait for a connection
        bool bHasPending = false;
        ListenerSocket->HasPendingConnection(bHasPending);
        if (!bHasPending)
        {
            FPlatformProcess::Sleep(0.1f);
            continue;
        }

        ConnectionSocket = ListenerSocket->Accept(TEXT("Client"));
        UE_LOG(LogTemp, Warning, TEXT("SocketReceiver: Client connected"));

        while (!bShouldStop)
        {
            // Header: [int32 Count][int32 Type]
            uint8 Header[8];
            if (!ReadExact(ConnectionSocket, Header, 8))
            {
                UE_LOG(LogTemp, Warning, TEXT("SocketReceiver: Connection closed"));
                break;
            }

            int32 Count = *reinterpret_cast<int32*>(Header);
            int32 Type  = *reinterpret_cast<int32*>(Header + 4);

            if (Count < 0 || Count > 1024 * 1024)
            {
                UE_LOG(LogTemp, Error, TEXT("SocketReceiver: Bad count %d"), Count);
                break;
            }

            TArray<uint8> Payload;
            Payload.SetNumUninitialized(Count + 1);  // +1 for null terminator
            Payload[Count] = 0;

            if (Count > 0 && !ReadExact(ConnectionSocket, Payload.GetData(), Count))
            {
                UE_LOG(LogTemp, Warning, TEXT("SocketReceiver: Failed to read payload"));
                break;
            }

            FString Message = UTF8_TO_TCHAR(reinterpret_cast<const char*>(Payload.GetData()));
            UE_LOG(LogTemp, Warning, TEXT("SocketReceiver: Type=%d  \"%s\""), Type, *Message);

            // Send ACK
            int32 Ack = 0;
            int32 BytesSent = 0;
            ConnectionSocket->Send(reinterpret_cast<uint8*>(&Ack), sizeof(Ack), BytesSent);
        }

        ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
        ConnectionSocket->Close();
        SS->DestroySocket(ConnectionSocket);
        ConnectionSocket = nullptr;
    }

    return 0;
}

void FSocketReceiverRunnable::Stop()
{
    bShouldStop = true;
    ISocketSubsystem* SS = ISocketSubsystem::Get(PLATFORM_SOCKETSUBSYSTEM);
    if (ConnectionSocket) { ConnectionSocket->Close(); SS->DestroySocket(ConnectionSocket); ConnectionSocket = nullptr; }
    if (ListenerSocket)   { ListenerSocket->Close();   SS->DestroySocket(ListenerSocket);   ListenerSocket = nullptr; }
}

bool FSocketReceiverRunnable::ReadExact(FSocket* Socket, uint8* Buffer, int32 NumBytes)
{
    int32 TotalRead = 0;
    while (TotalRead < NumBytes && !bShouldStop)
    {
        int32 BytesRead = 0;
        if (!Socket->Recv(Buffer + TotalRead, NumBytes - TotalRead, BytesRead))
            return false;
        if (BytesRead == 0)
            FPlatformProcess::Sleep(0.001f);
        TotalRead += BytesRead;
    }
    return TotalRead == NumBytes;
}

#pragma once
#include "CoreMinimal.h"
#include "HAL/Runnable.h"

class FSocketReceiverRunnable : public FRunnable
{
public:
    FSocketReceiverRunnable(int32 InPort);
    virtual ~FSocketReceiverRunnable();

    virtual bool Init() override;
    virtual uint32 Run() override;
    virtual void Stop() override;

private:
    int32 Port;
    FSocket* ListenerSocket = nullptr;
    FSocket* ConnectionSocket = nullptr;
    TAtomic<bool> bShouldStop{false};

    bool ReadExact(FSocket* Socket, uint8* Buffer, int32 NumBytes);
};

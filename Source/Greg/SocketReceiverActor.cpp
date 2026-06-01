#include "SocketReceiverActor.h"
#include "SocketReceiverRunnable.h"
#include "HAL/RunnableThread.h"

void ASocketReceiverActor::BeginPlay()
{
    Super::BeginPlay();
    Runnable = new FSocketReceiverRunnable(Port);
    Thread = FRunnableThread::Create(Runnable, TEXT("SocketReceiverThread"));
}

void ASocketReceiverActor::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
    if (Runnable) Runnable->Stop();
    if (Thread)   { Thread->WaitForCompletion(); delete Thread; Thread = nullptr; }
    if (Runnable) { delete Runnable; Runnable = nullptr; }
    Super::EndPlay(EndPlayReason);
}

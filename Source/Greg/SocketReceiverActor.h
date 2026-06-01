#pragma once
#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "SocketReceiverActor.generated.h"

class FSocketReceiverRunnable;

UCLASS()
class GREG_API ASocketReceiverActor : public AActor
{
    GENERATED_BODY()
public:
    UPROPERTY(EditDefaultsOnly, Category="Socket")
    int32 Port = 9001;

protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type EndPlayReason) override;

private:
    FSocketReceiverRunnable* Runnable = nullptr;
    FRunnableThread* Thread = nullptr;
};

# sanity check, prevent doing ridiculous PoW
# 20 million PoWs equals approximately 2 days on dev's dual R9 290
ridiculousDifficulty = 20000000

#If changed, these values will cause particularly unexpected behavior: You won't be able to either send or receive messages because the proof of work you do (or demand) won't match that done or demanded by others. Don't change them!
networkDefaultProofOfWorkNonceTrialsPerByte = 1000 #The amount of work that should be performed (and demanded) per byte of the payload.
networkDefaultPayloadLengthExtraBytes = 1000 #To make sending short messages a little more difficult, this value is added to the payload length for use in calculating the proof of work target.


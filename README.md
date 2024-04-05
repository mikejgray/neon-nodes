# Neon Nodes
Clients for connecting to a server running Hana. These are minimal classes that
are responsible for collecting a user's input, sending it to a remote system for
processing, and presenting a response to the user.

## Voice Client
The voice client will start a service that listens for a wake word on the local
system, sends recorded speech to a HANA endpoint for processing, and plays back
the response.

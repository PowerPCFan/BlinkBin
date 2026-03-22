# BlinkBin

A simple pastebin service with an open API designed to be simple and accessible for all developers. Create, retrieve, edit, and delete pastes without authentication, protected by rate limiting, proof-of-work, and temporary tokens.

https://bin.blinkl.ink/

Note: I'm eventually probably going to turn this into an API only and then make the website a separate thing that interfaces with the API, since right now I have the homepage + paste view page combined with the API which is fine but it's kinda fragile, and if I want to expand on this more in the future I'm gonna need something more stable and robust than like injecting html in python strings and returning it lol

# VESP-s_esp32_video_streamer

(DISCLAIMER: The code was written by AI)

This is the V-ESP (pronounced VESP like in vespa) It stands for Video ESP32 Streaming. The S is hidden. This was created for the benefit of all (and me). :) As long as the other was not made by AI as well. I want to support humans. But I can't code very well...
The max resolution is 320x240 because if I went higher you could only get like 10fps. at 320x240 you can only really get a max of 20. depending on what video you are playing.

Here is the list of parts that you will need to pull with off:
- ESP32
- TFT display
- MAX98357A Audio Amp
- Speaker
- And a way to power it.

Or alternatively:
- CYD
- MAX98357A Audio Amp
- Speaker
- And a way to power it.

The code is quite hard to work with right now. but at least all you have to do is upload it and get the ip address from the esp32 after typing in the wifi name and password. Then you put the ip address into the Player on your computer and BAM! You have it saved in the player_data file that the player makes. it just stores little bits of info to remember.

Here is a screenshot of the windows version of the program to stream videos:
<img width="1920" height="1031" alt="WIN_CYD_SCREEN" src="https://github.com/user-attachments/assets/44453e17-f761-4284-b5a8-eeeef9c62473" />

As you can see, I have the basic functionality there. Play, (non working pause...), Stop, Forward, Reverse. All the works. There are a couple of things that are not working on it yet. But we'll get to those later. The Serial monitor on the side takes basic values and displays them. And yes the Receiving ones are coming from the ESP32 over wifi. Not from a cable or whatever the pc itself computes. The image is as crisp as you can expect from such a low res display and it is decent audio. The wiring is simple. I can't get you the wiring for the TFT and ESP32 but I can get you the one for my CYD. And you get my Arduino IDE user_setup file for TFT.eSPI.h library. But you might have to change it for your tft display. But it is wired for my CYD.

MAX98357A
- VIN to 3.3v
- GND to GND
- SD to VIN
- GAIN to VIN
- DIN to GP23
- BCLK to GP18
- LRC to GP19
- and then the speaker outputs to the speaker.

And that's it! That's all you have to do to get sound and video with a CYD. Of course you might want to supply power with a USB-C cable or battery...

I hope you can enjoy using this as well. If you have any suggestions on changes or additions please let me know, if you hate it please don't let me know or say it kindly and tell me how I can try to fix it.

Things I want to add and fix
1. PLaylist functionality
2. Upload functionality
3. Buttons on ESP32
4. SD card functionality?
5. Custom Resolutions and make it so that it can go in a specific corner of the screen
6. Custom FPS
7. Brightness and contrast for fun (ESP32 side code)
8. Screen share (pc to esp)
9. Make the code more concise then what the AI did...
10. And make it friendly for all!
11. Play youtube URLS or the like.
12. Maybe more...
13. logo
14. more platform availibility

I hope you enjoy and please don't hesitate to ask questions. Thanks and enjoy! If you can make this into a mac application as well please do...

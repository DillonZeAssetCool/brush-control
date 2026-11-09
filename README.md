# Brush Control System

Control software for the chemistry conductor cleaning testing rig, running on a Raspberry Pi connected to an Artillery 3D motion platform.

---

## Quick Start Guide

Follow these simple steps on your computer to connect to the machine and start the application.

### Prerequisites
Make sure **Visual Studio Code (VS Code)** is installed on your computer and your computer is connected to the same local network (Wi-Fi or Ethernet) as the Raspberry Pi.

---

### Step 1: Install the Remote SSH Extension in VS Code
1. Open **VS Code** on your computer.
2. Click the **Extensions** icon on the left sidebar (looks like four blocks, or press `Ctrl + Shift + X` / `Cmd + Shift + X`).
3. Search for **Remote - SSH** (published by Microsoft).
4. Click **Install**.

*Verification:* A small blue icon (two brackets: `><`) will appear in the bottom-left corner of your VS Code window.

---

### Step 2: Connect to the Raspberry Pi
1. Click the blue icon (`><`) in the bottom-left corner, or click the **Remote Explorer** tab on the left sidebar.
2. Select **Connect to Host...** (or click the **+** button next to SSH).
3. In the text box that appears at the top of the window, type:
   ```text
   ssh pi@naboo
   ```
4. Press entered.
5. When prompted, type the password:
   ```text
   admin
   ```
   
---

### Step 3: Open the Terminal & Launch the System
1. In VS Code, open a new terminal window by clicking **Terminal** > **New Terminal** from the top menu (or press `Ctrl + ` ` / `Cmd + ` `).
2. Check the command prompt text to confirm you are connected to the Raspberry Pi.
   * *Verification:* You should see a green prompt starting with `user@Naboo:~ $` (or your Raspberry Pi's username).
3. Type the start command and press **Enter**:
   ```bash
   brushctrl

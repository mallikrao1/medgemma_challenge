---
title: MedGemma Discharge Copilot
emoji: 🏥
colorFrom: blue
colorTo: green
sdk: gradio
sdk_version: 5.49.1
app_file: app.py
pinned: false
---

# MedGemma Discharge Copilot (Public Demo)

This Space is a frontend that calls the deployed backend API.

## Setup

1. Create a new Hugging Face Space (Gradio SDK).
2. Upload all files from this folder.
3. In the app UI, set `Backend URL` to your AWS App Runner URL.

## HAI-DEF model trace

This project uses MedGemma as the target open healthcare model:

- Base model reference: `google/medgemma-4b-it` (HAI-DEF family)

If you publish your own fine-tuned adapter/model, replace this with your HF model URL in Kaggle writeup bonus link.


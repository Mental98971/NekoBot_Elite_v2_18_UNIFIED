"""Persona fine-tuning toolkit (offline; never imported by the running bot).

prepare_sft  -> raw exports -> private per-person train/val/test + manifest
train_lora   -> LoRA / QLoRA weight training on the train/val files (GPU box)
generate     -> predictions on the held-out test split (base model or base + adapter)
baselines    -> offline reference systems (retrieval, modal reply) for comparison
evaluate     -> metrics + gates for any prediction file
promote      -> mark an evaluated adapter ready for the bot
revoke       -> withdraw authorization and delete a person's adapters
"""

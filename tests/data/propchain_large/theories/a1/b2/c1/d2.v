Require Import prelude_import.
Theorem th98 (A0 A1 A2 A3 A4: Prop) : (A0->A1->A2->A3->A4->A3). intros. apply H2. Qed.
Theorem th99 (A0 A1 A2 A3 A4: Prop) : (A0->A1->A2->A3->A4->A4). intros. apply H3. Qed.
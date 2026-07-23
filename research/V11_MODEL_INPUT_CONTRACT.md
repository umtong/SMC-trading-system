# V11 causal input contract

At signal time only the completed 5-minute bar and fields timestamped at or before that bar close are admissible. Rolling baselines use trailing observations only. Future-return and future-direction fields are removed before feature selection. A trade may enter no earlier than the following 5-minute open. Event labels and exit outcomes are used only after their recorded exit time enters the training window.

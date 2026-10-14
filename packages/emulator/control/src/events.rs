//! Event fan-out from the controller to any number of subscribers (socket
//! clients, the desktop host). Each subscriber has a bounded queue; when it
//! is full the oldest information is dropped and a `lagged` event with the
//! count follows as soon as there is room, so a script can treat it as a
//! failure.

use std::sync::{Arc, Mutex, mpsc};

use serde_json::{Value, json};

/// Queue length per subscriber.
pub const QUEUE: usize = 1024;

struct Subscriber {
    tx: mpsc::SyncSender<Value>,
    kinds: Option<Vec<String>>,
    dropped: u64,
}

#[derive(Clone, Default)]
pub struct Events {
    inner: Arc<Mutex<Vec<Subscriber>>>,
}

impl Events {
    /// Receive every event, or only those whose `kind` is listed.
    pub fn subscribe(&self, kinds: Option<Vec<String>>) -> mpsc::Receiver<Value> {
        let (tx, rx) = mpsc::sync_channel(QUEUE);
        if let Ok(mut subs) = self.inner.lock() {
            subs.push(Subscriber {
                tx,
                kinds,
                dropped: 0,
            });
        }
        rx
    }

    /// Deliver `event` (an object with a `kind`) to the subscribers; those
    /// that went away are forgotten.
    pub fn emit(&self, event: Value) {
        let kind = event["kind"].as_str().unwrap_or("").to_string();
        let Ok(mut subs) = self.inner.lock() else {
            return;
        };
        subs.retain_mut(|sub| {
            if sub
                .kinds
                .as_ref()
                .is_some_and(|kinds| !kinds.iter().any(|k| *k == kind))
            {
                return true;
            }
            if sub.dropped > 0 {
                match sub
                    .tx
                    .try_send(json!({"kind": "lagged", "dropped": sub.dropped}))
                {
                    Ok(()) => sub.dropped = 0,
                    Err(mpsc::TrySendError::Full(_)) => {
                        sub.dropped += 1;
                        return true;
                    }
                    Err(mpsc::TrySendError::Disconnected(_)) => return false,
                }
            }
            match sub.tx.try_send(event.clone()) {
                Ok(()) => true,
                Err(mpsc::TrySendError::Full(_)) => {
                    sub.dropped += 1;
                    true
                }
                Err(mpsc::TrySendError::Disconnected(_)) => false,
            }
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filters_by_kind_and_reports_overflow_as_lagged() {
        let events = Events::default();
        let all = events.subscribe(None);
        let only = events.subscribe(Some(vec!["b".into()]));
        events.emit(json!({"kind": "a"}));
        events.emit(json!({"kind": "b"}));
        assert_eq!(all.try_recv().unwrap()["kind"], "a");
        assert_eq!(all.try_recv().unwrap()["kind"], "b");
        assert_eq!(only.try_recv().unwrap()["kind"], "b");
        assert!(only.try_recv().is_err());
        for _ in 0..QUEUE + 3 {
            events.emit(json!({"kind": "b"}));
        }
        let mut seen = 0;
        while only.try_recv().is_ok() {
            seen += 1;
        }
        assert_eq!(seen, QUEUE);
        events.emit(json!({"kind": "b"}));
        assert_eq!(only.try_recv().unwrap()["kind"], "lagged");
        assert_eq!(only.try_recv().unwrap()["kind"], "b");
        drop(only);
        events.emit(json!({"kind": "a"}));
    }
}

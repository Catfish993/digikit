use dkupload::mock::{MockDevice, MockTransport};
use dkupload::*;

fn data(n: usize, salt: u8) -> Vec<u8> {
    (0..n)
        .map(|i| (i as u32).wrapping_mul(2654435761).to_be_bytes()[0] ^ salt)
        .collect()
}
fn session(cap: u64) -> Session<MockTransport> {
    let mut s = Session::new(MockTransport::new(MockDevice::new(cap)));
    s.timeout = std::time::Duration::from_millis(5);
    s
}
fn put(s: &mut Session<MockTransport>, path: &str, d: Vec<u8>) -> Result<(), Error> {
    s.put(path, d, &mut |_| true)
}

#[test]
fn info_list_put_delete() {
    let mut s = session(1 << 20);
    let i = s.info().unwrap();
    assert_eq!(
        (i.protocol, i.max_chunk, i.port.as_str()),
        (1, 32768, "mock-0.0")
    );
    // sizes: empty, one byte, exact chunk, chunk + short tail, several chunks
    for (k, n) in [0usize, 1, 512, 32768, 32769, 100_000]
        .into_iter()
        .enumerate()
    {
        put(&mut s, &format!("modules/f{k}.dkm"), data(n, k as u8)).unwrap();
    }
    let list = s.list().unwrap();
    assert_eq!(list.len(), 6);
    for (k, n) in [0usize, 1, 512, 32768, 32769, 100_000]
        .into_iter()
        .enumerate()
    {
        let e = list
            .iter()
            .find(|e| e.path == format!("modules/f{k}.dkm"))
            .unwrap();
        assert_eq!(e.size as usize, n);
        assert_eq!(e.sha256, sha256(&data(n, k as u8)));
    }
    s.delete("modules/f1.dkm").unwrap();
    assert_eq!(s.list().unwrap().len(), 5);
    assert!(matches!(
        s.delete("modules/f1.dkm"),
        Err(Error::Device {
            status: Status::NoFile,
            ..
        })
    ));
}

#[test]
fn list_is_paged() {
    let mut s = session(1 << 22);
    for k in 0..40 {
        put(
            &mut s,
            &format!("modules/a-fairly-long-module-name-{k:02}.dkm"),
            data(10, k),
        )
        .unwrap();
    }
    let log_before = s.transport.dev.log.iter().filter(|&&c| c == 0x02).count();
    assert_eq!(s.list().unwrap().len(), 40);
    assert!(s.transport.dev.log.iter().filter(|&&c| c == 0x02).count() - log_before > 1);
}

#[test]
fn at_most_two_in_flight_and_in_order() {
    // Drive the machine by hand: count requests sent before the first reply.
    let mut m = PutMachine::new("modules/x.dkm", data(5 * 32768, 1), 32768, 0).unwrap();
    let mut dev = MockDevice::new(1 << 20);
    let begin = m.next_send().unwrap();
    assert!(m.next_send().is_none());
    m.on_reply(&decode(&dev.handle(&begin).unwrap()).unwrap())
        .unwrap();
    let mut flying = vec![];
    while let Some(msg) = m.next_send() {
        flying.push(msg);
    }
    assert_eq!(flying.len(), 2);
    let mut max = 2;
    let mut sent = 2;
    while !m.is_done() {
        let first = flying.remove(0);
        m.on_reply(&decode(&dev.handle(&first).unwrap()).unwrap())
            .unwrap();
        while let Some(msg) = m.next_send() {
            flying.push(msg);
            sent += 1;
        }
        max = max.max(flying.len());
        if flying.is_empty() && !m.is_done() {
            unreachable!();
        }
        if m.phase() == dkupload::put::Phase::End && flying.len() == 1 {
            // END goes out only after every DATA reply
            assert_eq!(decode(&flying[0]).unwrap().cmd, 0x12);
        }
    }
    assert_eq!(max, 2);
    assert_eq!(sent, 5 + 1);
    assert_eq!(dev.files.len(), 1);
}

#[test]
fn lost_reply_is_retried_by_offset() {
    let mut s = session(1 << 20);
    let d = data(3 * 32768 + 100, 3);
    // After BEGIN: lose the reply of the first DATA.
    s.transport.dev.drop_replies = 0;
    put(&mut s, "modules/ok.dkm", data(10, 0)).unwrap();
    s.transport.dev.drop_replies = 1; // BEGIN's reply
    put(&mut s, "modules/lost-begin.dkm", d.clone()).unwrap();
    s.transport.dev.drop_requests = 2; // BEGIN and first DATA lost... first request only
    put(&mut s, "modules/lost-req.dkm", d.clone()).unwrap();
    let f = &s.transport.dev.files;
    assert_eq!(
        f.iter().find(|x| x.0 == "modules/lost-req.dkm").unwrap().1,
        d
    );
    assert_eq!(
        f.iter()
            .find(|x| x.0 == "modules/lost-begin.dkm")
            .unwrap()
            .1,
        d
    );
}

#[test]
fn lost_data_reply_mid_stream() {
    let mut s = session(1 << 20);
    let d = data(6 * 32768, 5);
    // Replies are lost one at a time at different points: BEGIN, then DATA k.
    for skip in 1..=7u32 {
        let mut dev = MockDevice::new(1 << 20);
        let mut t = MockTransport::new(std::mem::replace(&mut dev, MockDevice::new(0)));
        // lose the reply number `skip` (counting BEGIN as 1)
        struct Lossy(MockTransport, u32, bool);
        impl Transport for Lossy {
            fn send(&mut self, m: &[u8]) -> Result<(), String> {
                self.0.send(m)
            }
            fn recv(&mut self, to: std::time::Duration) -> Result<Option<Vec<u8>>, String> {
                let r = self.0.recv(to)?;
                if r.is_some() {
                    self.1 = self.1.saturating_sub(1);
                    if self.1 == 0 && !self.2 {
                        self.2 = true;
                        return Ok(None);
                    }
                }
                Ok(r)
            }
        }
        t.dev.capacity = 1 << 20;
        let mut l = Session::new(Lossy(t, skip, false));
        l.timeout = std::time::Duration::from_millis(5);
        // A lost reply with 2 in flight can make the later chunk fail with E_OFFSET only if the earlier request
        // was lost too; here only replies are lost, so the upload must complete.
        l.put("modules/m.dkm", d.clone(), &mut |_| true).unwrap();
        assert_eq!(l.transport.0.dev.files[0].1, d, "skip {skip}");
    }
    let _ = &mut s;
}

#[test]
fn errors_full_hash_path_state() {
    let mut s = session(40_000);
    // E_FULL, carrying the free bytes
    match put(&mut s, "modules/big.dkm", data(100_000, 0)) {
        Err(Error::Device {
            status: Status::Full,
            free_bytes: Some(f),
        }) => {
            assert_eq!(f, s.transport.dev.free_bytes())
        }
        other => panic!("{other:?}"),
    }
    // E_PATH is refused by the host before sending; a raw bad path reaches the device
    assert!(matches!(
        put(&mut s, "a/../b", vec![1]),
        Err(Error::Request(_))
    ));
    let mut raw = b"a/../b\0".to_vec();
    raw.extend([0u8; 36]);
    let r = s.request(0x10, &raw);
    assert!(
        matches!(
            r,
            Err(Error::Device {
                status: Status::Path,
                ..
            })
        ),
        "{r:?}"
    );
    // E_STATE
    let r = s.request(0x12, &[]);
    assert!(matches!(
        r,
        Err(Error::Device {
            status: Status::State,
            ..
        })
    ));
    // E_HASH: BEGIN with a wrong sha, DATA, END
    let body = proto::begin_body("modules/h.dkm", 3, &[7u8; 32]).unwrap();
    s.request(0x10, &body).unwrap();
    s.request(0x11, &proto::data_body(0, &[1, 2, 3])).unwrap();
    assert!(matches!(
        s.request(0x12, &[]),
        Err(Error::Device {
            status: Status::Hash,
            ..
        })
    ));
    assert!(s.list().unwrap().is_empty());
    // E_OFFSET and E_LENGTH, and a retry of the previous chunk
    let body = proto::begin_body("modules/o.dkm", 2048, &sha256(&data(2048, 1))).unwrap();
    s.request(0x10, &body).unwrap();
    assert!(matches!(
        s.request(0x11, &proto::data_body(512, &data(512, 1))),
        Err(Error::Device {
            status: Status::Offset,
            ..
        })
    ));
    assert!(matches!(
        s.request(0x11, &proto::data_body(0, &data(100, 1))),
        Err(Error::Device {
            status: Status::Length,
            ..
        })
    ));
    s.request(0x11, &proto::data_body(0, &data(1024, 1)[..1024]))
        .unwrap();
    s.request(0x11, &proto::data_body(0, &data(1024, 1)[..1024]))
        .unwrap(); // retry
    assert!(matches!(
        s.request(0x11, &proto::data_body(0, &data(512, 1))),
        Err(Error::Device {
            status: Status::Offset,
            ..
        })
    ));
    // bad protocol version
    let mut bad = encode_request(0x01, 1, &[]);
    bad[4] = 2;
    assert_eq!(
        decode(&s.transport.dev.handle(&bad).unwrap()).unwrap().body,
        vec![(-60i8) as u8]
    );
}

#[test]
fn repack_frees_space() {
    let mut s = session(100_000);
    let a = data(30_000, 1);
    let b = data(30_000, 2);
    put(&mut s, "modules/a.dkm", a.clone()).unwrap();
    put(&mut s, "modules/b.dkm", b.clone()).unwrap();
    // Replacing a keeps its old data until CLEAR: a replacement fits once, the next one does not.
    put(&mut s, "modules/a.dkm", a.clone()).unwrap();
    assert!(matches!(
        put(&mut s, "modules/b.dkm", b.clone()),
        Err(Error::Device {
            status: Status::Full,
            ..
        })
    ));
    let listing = s.list().unwrap();
    let lib = vec![("modules/a.dkm".to_string(), sha256(&a))];
    assert_eq!(
        repack_missing(&listing, &lib),
        vec!["modules/b.dkm".to_string()]
    );
    let lib = vec![
        ("modules/a.dkm".to_string(), sha256(&a)),
        ("modules/b.dkm".to_string(), sha256(&b)),
    ];
    assert!(repack_missing(&listing, &lib).is_empty());
    let free_before = s.info().unwrap().free_bytes;
    s.repack(
        vec![
            ("modules/a.dkm".into(), a.clone()),
            ("modules/b.dkm".into(), b.clone()),
        ],
        &mut |_, _| true,
    )
    .unwrap();
    assert!(s.info().unwrap().free_bytes > free_before);
    put(&mut s, "modules/b.dkm", b).unwrap();
}

#[test]
fn cancel_aborts() {
    let mut s = session(1 << 20);
    let mut n = 0;
    let r = s.put("modules/c.dkm", data(200_000, 0), &mut |_| {
        n += 1;
        n < 3
    });
    assert_eq!(r, Err(Error::Cancelled));
    assert!(!s.info().unwrap().upload_open());
    assert!(s.list().unwrap().is_empty());
}

#[test]
fn no_store_and_clear() {
    let mut s = session(1 << 20);
    s.transport.dev.no_store = true;
    assert!(matches!(
        s.list(),
        Err(Error::Device {
            status: Status::NoStore,
            ..
        })
    ));
    s.clear().unwrap();
    assert!(s.list().unwrap().is_empty());
}

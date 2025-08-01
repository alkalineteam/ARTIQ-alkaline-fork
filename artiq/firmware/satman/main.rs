#![feature(never_type, panic_info_message, asm, default_alloc_error_handler)]
#![no_std]

#[macro_use]
extern crate log;
#[macro_use]
extern crate board_misoc;
extern crate board_artiq;
extern crate logger_artiq;
extern crate riscv;
extern crate alloc;
extern crate proto_artiq;
extern crate byteorder;
extern crate crc;
extern crate cslice;
extern crate io;
extern crate eh;

use core::convert::TryFrom;
use board_misoc::{csr, ident, clock, config, i2c, pmp};
#[cfg(has_si5324)]
use board_artiq::si5324;
#[cfg(has_si549)]
use board_artiq::si549;
#[cfg(soc_platform = "kasli")]
use board_misoc::irq;
use board_misoc::{boot, spiflash};
use board_artiq::{spi, drtioaux, drtio_routing};
#[cfg(soc_platform = "efc")]
use board_artiq::ad9117;
use proto_artiq::drtioaux_proto::{SAT_PAYLOAD_MAX_SIZE, MASTER_PAYLOAD_MAX_SIZE, CXP_PAYLOAD_MAX_SIZE};
#[cfg(has_drtio_eem)]
use board_artiq::drtio_eem;
use riscv::register::{mcause, mepc, mtval};
use dma::Manager as DmaManager;
use kernel::Manager as KernelManager;
use mgmt::Manager as CoreManager;
use analyzer::Analyzer;

#[global_allocator]
static mut ALLOC: alloc_list::ListAlloc = alloc_list::EMPTY;

mod repeater;
mod routing;
mod dma;
mod analyzer;
mod kernel;
mod cache;
mod mgmt;

fn drtiosat_reset(reset: bool) {
    unsafe {
        csr::drtiosat::reset_write(if reset { 1 } else { 0 });
    }
}

fn drtiosat_reset_phy(reset: bool) {
    unsafe {
        csr::drtiosat::reset_phy_write(if reset { 1 } else { 0 });
    }
}

fn drtiosat_link_rx_up() -> bool {
    unsafe {
        csr::drtiosat::rx_up_read() == 1
    }
}

fn drtiosat_tsc_loaded() -> bool {
    unsafe {
        let tsc_loaded = csr::drtiosat::tsc_loaded_read() == 1;
        if tsc_loaded {
            csr::drtiosat::tsc_loaded_write(1);
        }
        tsc_loaded
    }
}

fn toggle_sed_spread(val: u8) {
    unsafe { csr::drtiosat::sed_spread_enable_write(val); }
}

#[derive(Clone, Copy)]
pub enum RtioMaster {
    Drtio,
    Dma,
    Kernel
}

pub fn cricon_select(master: RtioMaster) {
    let val = match master {
        RtioMaster::Drtio => 0,
        RtioMaster::Dma => 1,
        RtioMaster::Kernel => 2
    };
    unsafe {
        csr::cri_con::selected_write(val);
    }
}

pub fn cricon_read() -> RtioMaster {
    let val = unsafe { csr::cri_con::selected_read() };
    match val {
        0 => RtioMaster::Drtio,
        1 => RtioMaster::Dma,
        2 => RtioMaster::Kernel,
        _ => unreachable!()
    }
}

#[cfg(has_drtio_routing)]
macro_rules! forward {
    ($router:expr, $routing_table:expr, $destination:expr, $rank:expr, $self_destination:expr, $repeaters:expr, $packet:expr) => {{
        let hop = $routing_table.0[$destination as usize][$rank as usize];
        if hop != 0 {
            let repno = (hop - 1) as usize;
            if repno < $repeaters.len() {
                if $packet.expects_response() {
                    return $repeaters[repno].aux_forward($packet, $router, $routing_table, $rank, $self_destination);
                } else {
                    let res = $repeaters[repno].aux_send($packet);
                    // allow the satellite to parse the packet before next
                    clock::spin_us(10_000);
                    return res;
                }
            } else {
                return Err(drtioaux::Error::RoutingError);
            }
        }
    }}
}

#[cfg(not(has_drtio_routing))]
macro_rules! forward {
    ($router:expr, $routing_table:expr, $destination:expr, $rank:expr, $self_destination:expr, $repeaters:expr, $packet:expr) => {}
}

fn process_aux_packet(dmamgr: &mut DmaManager, analyzer: &mut Analyzer, kernelmgr: &mut KernelManager, coremgr: &mut CoreManager,
        _repeaters: &mut [repeater::Repeater], _routing_table: &mut drtio_routing::RoutingTable, rank: &mut u8,
        router: &mut routing::Router, self_destination: &mut u8, packet: drtioaux::Packet
) -> Result<(), drtioaux::Error<!>> {
    // In the code below, *_chan_sel_write takes an u8 if there are fewer than 256 channels,
    // and u16 otherwise; hence the `as _` conversion.
    match packet {
        drtioaux::Packet::EchoRequest =>
            drtioaux::send(0, &drtioaux::Packet::EchoReply),
        drtioaux::Packet::ResetRequest => {
            info!("resetting RTIO");
            drtiosat_reset(true);
            clock::spin_us(100);
            drtiosat_reset(false);
            for rep in _repeaters.iter() {
                if let Err(e) = rep.rtio_reset() {
                    error!("failed to issue RTIO reset ({})", e);
                }
            }
            drtioaux::send(0, &drtioaux::Packet::ResetAck)
        },

        drtioaux::Packet::DestinationStatusRequest { destination } => {
            #[cfg(has_drtio_routing)]
            let hop = _routing_table.0[destination as usize][*rank as usize];
            #[cfg(not(has_drtio_routing))]
            let hop = 0;

            if hop == 0 {
                *self_destination = destination;
                let errors;
                unsafe {
                    errors = csr::drtiosat::rtio_error_read();
                }
                if errors & 1 != 0 {
                    let channel;
                    unsafe {
                        channel = csr::drtiosat::sequence_error_channel_read();
                        csr::drtiosat::rtio_error_write(1);
                    }
                    drtioaux::send(0,
                        &drtioaux::Packet::DestinationSequenceErrorReply { channel })?;
                } else if errors & 2 != 0 {
                    let channel;
                    unsafe {
                        channel = csr::drtiosat::collision_channel_read();
                        csr::drtiosat::rtio_error_write(2);
                    }
                    drtioaux::send(0,
                        &drtioaux::Packet::DestinationCollisionReply { channel })?;
                } else if errors & 4 != 0 {
                    let channel;
                    unsafe {
                        channel = csr::drtiosat::busy_channel_read();
                        csr::drtiosat::rtio_error_write(4);
                    }
                    drtioaux::send(0,
                        &drtioaux::Packet::DestinationBusyReply { channel })?;
                }
                else {
                    drtioaux::send(0, &drtioaux::Packet::DestinationOkReply)?;
                }
            }

            #[cfg(has_drtio_routing)]
            {
                if hop != 0 {
                    let hop = hop as usize;
                    if hop <= csr::DRTIOREP.len() {
                        let repno = hop - 1;
                        match _repeaters[repno].aux_forward(&drtioaux::Packet::DestinationStatusRequest {
                            destination: destination
                        }, router, _routing_table, *rank, *self_destination) {
                            Ok(()) => (),
                            Err(drtioaux::Error::LinkDown) => drtioaux::send(0, &drtioaux::Packet::DestinationDownReply)?,
                            Err(e) => {
                                drtioaux::send(0, &drtioaux::Packet::DestinationDownReply)?;
                                error!("aux error when handling destination status request: {}", e);
                            },
                        }
                    } else {
                        drtioaux::send(0, &drtioaux::Packet::DestinationDownReply)?;
                    }
                }
            }
            Ok(())
        }

        #[cfg(has_drtio_routing)]
        drtioaux::Packet::RoutingSetPath { destination, hops } => {
            _routing_table.0[destination as usize] = hops;
            for rep in _repeaters.iter() {
                if let Err(e) = rep.set_path(destination, &hops) {
                    error!("failed to set path ({})", e);
                }
            }
            drtioaux::send(0, &drtioaux::Packet::RoutingAck)
        }
        #[cfg(has_drtio_routing)]
        drtioaux::Packet::RoutingSetRank { rank: new_rank } => {
            *rank = new_rank;
            drtio_routing::interconnect_enable_all(_routing_table, new_rank);

            let rep_rank = new_rank + 1;
            for rep in _repeaters.iter() {
                if let Err(e) = rep.set_rank(rep_rank) {
                    error!("failed to set rank ({})", e);
                }
            }

            info!("rank: {}", new_rank);
            info!("routing table: {}", _routing_table);

            drtioaux::send(0, &drtioaux::Packet::RoutingAck)
        }

        #[cfg(not(has_drtio_routing))]
        drtioaux::Packet::RoutingSetPath { destination: _, hops: _ } => {
            drtioaux::send(0, &drtioaux::Packet::RoutingAck)
        }
        #[cfg(not(has_drtio_routing))]
        drtioaux::Packet::RoutingSetRank { rank: _ } => {
            drtioaux::send(0, &drtioaux::Packet::RoutingAck)
        }

        drtioaux::Packet::MonitorRequest { destination: _destination, channel, probe } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let value;
            #[cfg(has_rtio_moninj)]
            unsafe {
                csr::rtio_moninj::mon_chan_sel_write(channel as _);
                csr::rtio_moninj::mon_probe_sel_write(probe);
                csr::rtio_moninj::mon_value_update_write(1);
                value = csr::rtio_moninj::mon_value_read() as u64;
            }
            #[cfg(not(has_rtio_moninj))]
            {
                value = 0;
            }
            let reply = drtioaux::Packet::MonitorReply { value: value };
            drtioaux::send(0, &reply)
        },
        drtioaux::Packet::InjectionRequest { destination: _destination, channel, overrd, value } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            #[cfg(has_rtio_moninj)]
            unsafe {
                csr::rtio_moninj::inj_chan_sel_write(channel as _);
                csr::rtio_moninj::inj_override_sel_write(overrd);
                csr::rtio_moninj::inj_value_write(value);
            }
            Ok(())
        },
        drtioaux::Packet::InjectionStatusRequest { destination: _destination, channel, overrd } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let value;
            #[cfg(has_rtio_moninj)]
            unsafe {
                csr::rtio_moninj::inj_chan_sel_write(channel as _);
                csr::rtio_moninj::inj_override_sel_write(overrd);
                value = csr::rtio_moninj::inj_value_read();
            }
            #[cfg(not(has_rtio_moninj))]
            {
                value = 0;
            }
            drtioaux::send(0, &drtioaux::Packet::InjectionStatusReply { value: value })
        },

        drtioaux::Packet::I2cStartRequest { destination: _destination, busno } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let succeeded = i2c::start(busno).is_ok();
            drtioaux::send(0, &drtioaux::Packet::I2cBasicReply { succeeded: succeeded })
        }
        drtioaux::Packet::I2cRestartRequest { destination: _destination, busno } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let succeeded = i2c::restart(busno).is_ok();
            drtioaux::send(0, &drtioaux::Packet::I2cBasicReply { succeeded: succeeded })
        }
        drtioaux::Packet::I2cStopRequest { destination: _destination, busno } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let succeeded = i2c::stop(busno).is_ok();
            drtioaux::send(0, &drtioaux::Packet::I2cBasicReply { succeeded: succeeded })
        }
        drtioaux::Packet::I2cWriteRequest { destination: _destination, busno, data } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            match i2c::write(busno, data) {
                Ok(_) => drtioaux::send(0,
                    &drtioaux::Packet::I2cWriteReply { succeeded: true, ack: true }),
                Err(i2c::Error::Nack) => drtioaux::send(0,
                    &drtioaux::Packet::I2cWriteReply { succeeded: true, ack: false }),
                Err(_) => drtioaux::send(0,
                    &drtioaux::Packet::I2cWriteReply { succeeded: false, ack: false })
            }
        }
        drtioaux::Packet::I2cReadRequest { destination: _destination, busno, ack } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            match i2c::read(busno, ack) {
                Ok(data) => drtioaux::send(0,
                    &drtioaux::Packet::I2cReadReply { succeeded: true, data: data }),
                Err(_) => drtioaux::send(0,
                    &drtioaux::Packet::I2cReadReply { succeeded: false, data: 0xff })
            }
        }
        drtioaux::Packet::I2cSwitchSelectRequest { destination: _destination, busno, address, mask } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let succeeded = i2c::switch_select(busno, address, mask).is_ok();
            drtioaux::send(0, &drtioaux::Packet::I2cBasicReply { succeeded: succeeded })
        }

        drtioaux::Packet::SpiSetConfigRequest { destination: _destination, busno, flags, length, div, cs } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let succeeded = spi::set_config(busno, flags, length, div, cs).is_ok();
            drtioaux::send(0,
                &drtioaux::Packet::SpiBasicReply { succeeded: succeeded })
        },
        drtioaux::Packet::SpiWriteRequest { destination: _destination, busno, data } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let succeeded = spi::write(busno, data).is_ok();
            drtioaux::send(0,
                &drtioaux::Packet::SpiBasicReply { succeeded: succeeded })
        }
        drtioaux::Packet::SpiReadRequest { destination: _destination, busno } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            match spi::read(busno) {
                Ok(data) => drtioaux::send(0,
                    &drtioaux::Packet::SpiReadReply { succeeded: true, data: data }),
                Err(_) => drtioaux::send(0,
                    &drtioaux::Packet::SpiReadReply { succeeded: false, data: 0 })
            }
        }

        drtioaux::Packet::AnalyzerHeaderRequest { destination: _destination } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let header = analyzer.get_header();
            drtioaux::send(0, &drtioaux::Packet::AnalyzerHeader {
                total_byte_count: header.total_byte_count,
                sent_bytes: header.sent_bytes,
                overflow_occurred: header.overflow,
            })
        }

        drtioaux::Packet::AnalyzerDataRequest { destination: _destination } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let mut data_slice: [u8; SAT_PAYLOAD_MAX_SIZE] = [0; SAT_PAYLOAD_MAX_SIZE];
            let meta = analyzer.get_data(&mut data_slice);
            drtioaux::send(0, &drtioaux::Packet::AnalyzerData {
                last: meta.last,
                length: meta.len,
                data: data_slice,
            })
        }

        drtioaux::Packet::DmaAddTraceRequest { source, destination, id, status, length, trace } => {
            forward!(router, _routing_table, destination, *rank, *self_destination, _repeaters, &packet);
            *self_destination = destination;
            let succeeded = dmamgr.add(source, id, status, &trace, length as usize).is_ok();
            router.send(drtioaux::Packet::DmaAddTraceReply { 
                source: *self_destination, destination: source, id: id, succeeded: succeeded 
            }, _routing_table, *rank, *self_destination)
        }
        drtioaux::Packet::DmaAddTraceReply { source, destination: _destination, id, succeeded } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            dmamgr.ack_upload(kernelmgr, source, id, succeeded, router, *rank, *self_destination, _routing_table);
            Ok(())
        }
        drtioaux::Packet::DmaRemoveTraceRequest { source, destination: _destination, id } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let succeeded = dmamgr.erase(source, id).is_ok();
            router.send(drtioaux::Packet::DmaRemoveTraceReply { 
                destination: source, succeeded: succeeded 
            }, _routing_table, *rank, *self_destination)
        }
        drtioaux::Packet::DmaPlaybackRequest { source, destination: _destination, id, timestamp } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            // no DMA with a running kernel
            let succeeded = !kernelmgr.is_running() && dmamgr.playback(source, id, timestamp).is_ok();
            router.send(drtioaux::Packet::DmaPlaybackReply { 
                destination: source, succeeded: succeeded
            }, _routing_table, *rank, *self_destination)
        }
        drtioaux::Packet::DmaPlaybackReply { destination: _destination, succeeded } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            if !succeeded {
                kernelmgr.ddma_nack();
            }
            Ok(())
        }
        drtioaux::Packet::DmaPlaybackStatus { source: _, destination: _destination, id, error, channel, timestamp } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            dmamgr.remote_finished(kernelmgr, id, error, channel, timestamp);
            Ok(())
        }

        drtioaux::Packet::SubkernelAddDataRequest { destination, id, status, length, data } => {
            forward!(router, _routing_table, destination, *rank, *self_destination, _repeaters, &packet);
            *self_destination = destination;
            let succeeded = kernelmgr.add(id, status, &data, length as usize).is_ok();
            drtioaux::send(0,
                &drtioaux::Packet::SubkernelAddDataReply { succeeded: succeeded })
        }
        drtioaux::Packet::SubkernelLoadRunRequest { source, destination: _destination, id, run, timestamp } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let mut succeeded = kernelmgr.load(id).is_ok();
            // allow preloading a kernel with delayed run
            if run {
                if dmamgr.running() {
                    // cannot run kernel while DDMA is running
                    succeeded = false;
                } else {
                    succeeded |= kernelmgr.run(source, id, timestamp).is_ok();
                }
            }
            router.send(drtioaux::Packet::SubkernelLoadRunReply { 
                    destination: source, succeeded: succeeded 
                }, 
            _routing_table, *rank, *self_destination)
        }
        drtioaux::Packet::SubkernelLoadRunReply { destination: _destination, succeeded } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            // received if local subkernel started another, remote subkernel
            kernelmgr.subkernel_load_run_reply(succeeded, *self_destination);
            Ok(())
        }
        drtioaux::Packet::SubkernelFinished { destination: _destination, id, with_exception, exception_src } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            kernelmgr.remote_subkernel_finished(id, with_exception, exception_src);
            Ok(())
        }
        drtioaux::Packet::SubkernelExceptionRequest { source, destination: _destination } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            let mut data_slice: [u8; MASTER_PAYLOAD_MAX_SIZE] = [0; MASTER_PAYLOAD_MAX_SIZE];
            let meta = kernelmgr.exception_get_slice(&mut data_slice);
            router.send(drtioaux::Packet::SubkernelException {
                destination: source,
                last: meta.status.is_last(),
                length: meta.len,
                data: data_slice,
            }, _routing_table, *rank, *self_destination)
        }
        drtioaux::Packet::SubkernelException { destination: _destination, last, length, data } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            kernelmgr.received_exception(&data[..length as usize], last, router, _routing_table, *rank, *self_destination);
            Ok(())
        }
        drtioaux::Packet::SubkernelMessage { source, destination: _destination, id, status, length, data } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            kernelmgr.message_handle_incoming(status, length as usize, id, &data);
            router.send(drtioaux::Packet::SubkernelMessageAck {
                    destination: source
                }, _routing_table, *rank, *self_destination)
        }
        drtioaux::Packet::SubkernelMessageAck { destination: _destination } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);
            if kernelmgr.message_ack_slice() {
                let mut data_slice: [u8; MASTER_PAYLOAD_MAX_SIZE] = [0; MASTER_PAYLOAD_MAX_SIZE];
                if let Some(meta) = kernelmgr.message_get_slice(&mut data_slice) {
                    // route and not send immediately as ACKs are not a beginning of a transaction
                    router.route(drtioaux::Packet::SubkernelMessage {
                        source: *self_destination, destination: meta.destination, id: kernelmgr.get_current_id().unwrap(),
                        status: meta.status, length: meta.len as u16, data: data_slice
                    }, _routing_table, *rank, *self_destination);
                } else {
                    error!("Error receiving message slice");
                }
            }
            Ok(())
        }

        drtioaux::Packet::CoreMgmtGetLogRequest { destination: _destination, clear } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            let mut data_slice = [0; SAT_PAYLOAD_MAX_SIZE];
            if let Ok(meta) = coremgr.log_get_slice(&mut data_slice, clear) {
                drtioaux::send(
                    0,
                    &drtioaux::Packet::CoreMgmtGetLogReply {
                        last: meta.status.is_last(),
                        length: meta.len as u16,
                        data: data_slice,
                    },
                )
            } else {
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: false })
            }
        }
        drtioaux::Packet::CoreMgmtClearLogRequest { destination: _destination } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: mgmt::clear_log().is_ok() })
        }
        drtioaux::Packet::CoreMgmtSetLogLevelRequest {destination: _destination, log_level } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            if let Ok(level_filter) = mgmt::byte_to_level_filter(log_level) {
                info!("changing log level to {}", level_filter);
                log::set_max_level(level_filter);
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: true })
            } else {
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: false })
            }
        }
        drtioaux::Packet::CoreMgmtSetUartLogLevelRequest { destination: _destination, log_level } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            if let Ok(level_filter) = mgmt::byte_to_level_filter(log_level) {
                info!("changing UART log level to {}", level_filter);
                logger_artiq::BufferLogger::with(|logger|
                    logger.set_uart_log_level(level_filter));
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: true })
            } else {
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: false })
            }
        }
        drtioaux::Packet::CoreMgmtConfigReadRequest {
            destination: _destination,
            length,
            key,
        } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            let mut value_slice = [0; SAT_PAYLOAD_MAX_SIZE];

            let key_slice = &key[..length as usize];
            if !key_slice.is_ascii() {
                error!("invalid key");
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: false })
            } else {
                let key = core::str::from_utf8(key_slice).unwrap();
                if coremgr.fetch_config_value(key).is_ok() {
                    let meta = coremgr.get_config_value_slice(&mut value_slice);
                    drtioaux::send(
                        0,
                        &drtioaux::Packet::CoreMgmtConfigReadReply {
                            length: meta.len as u16,
                            last: meta.status.is_last(),
                            value: value_slice,
                        },
                    )
                } else {
                    drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: false })
                }
            }
        }
        drtioaux::Packet::CoreMgmtConfigReadContinue {
            destination: _destination,
        } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            let mut value_slice = [0; SAT_PAYLOAD_MAX_SIZE];
            let meta = coremgr.get_config_value_slice(&mut value_slice);
            drtioaux::send(
                0,
                &drtioaux::Packet::CoreMgmtConfigReadReply {
                    length: meta.len as u16,
                    last: meta.status.is_last(),
                    value: value_slice,
                },
            )
        }
        drtioaux::Packet::CoreMgmtConfigWriteRequest { destination: _destination, last, length, data }  => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            coremgr.add_config_data(&data, length as usize);
            if last {
                coremgr.write_config()
            } else {
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: true })
            }
        }
        drtioaux::Packet::CoreMgmtConfigRemoveRequest { destination: _destination, length, key } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            let key = core::str::from_utf8(&key[..length as usize]).unwrap();
            let succeeded = config::remove(key)
                .map_err(|err| warn!("error on removing config: {:?}", err))
                .is_ok();

            drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded })
        }
        drtioaux::Packet::CoreMgmtConfigEraseRequest { destination: _destination } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            let succeeded = config::erase()
                .map_err(|err| warn!("error on erasing config: {:?}", err))
                .is_ok();

            drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded })
        }
        drtioaux::Packet::CoreMgmtRebootRequest { destination: _destination } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: true })?;
            warn!("restarting");
            unsafe { spiflash::reload(); }
        }
        drtioaux::Packet::CoreMgmtFlashRequest { destination: _destination, payload_length } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            coremgr.allocate_image_buffer(payload_length as usize);
            drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: true })
        }
        drtioaux::Packet::CoreMgmtFlashAddDataRequest { destination: _destination, last, length, data } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            coremgr.add_image_data(&data, length as usize);
            if last {
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtDropLink)
            } else {
                drtioaux::send(0, &drtioaux::Packet::CoreMgmtReply { succeeded: true })
            }
        }
        drtioaux::Packet::CoreMgmtDropLinkAck { destination: _destination } => {
            forward!(router, _routing_table, _destination, *rank, *self_destination, _repeaters, &packet);

            #[cfg(not(any(soc_platform = "efc", soc_platform = "phaser")))]
            unsafe {
                csr::gt_drtio::txenable_write(0);
            }

            #[cfg(has_drtio_eem)]
            unsafe {
                csr::eem_transceiver::txenable_write(0);
            }

            coremgr.flash_image();
            warn!("restarting");
            unsafe { spiflash::reload(); }
        }
        drtioaux::Packet::CXPReadRequest { destination: _destination, .. }
        | drtioaux::Packet::CXPWrite32Request { destination: _destination, .. }
        | drtioaux::Packet::CXPROIViewerSetupRequest { destination: _destination, .. }
        | drtioaux::Packet::CXPROIViewerDataRequest { destination: _destination } => {
            forward!(
                router,
                _routing_table,
                _destination,
                *rank,
                *self_destination,
                _repeaters,
                &packet
            );

            let err_msg = "Kasli doesn't support CoaXPress-SFP";
            error!("{}", err_msg);
            let length = err_msg.as_bytes().len();
            let mut message: [u8; CXP_PAYLOAD_MAX_SIZE] = [0; CXP_PAYLOAD_MAX_SIZE];
            message[..length].copy_from_slice(&err_msg.as_bytes());
            drtioaux::send(
                0,
                &drtioaux::Packet::CXPError {
                    length: length as u16,
                    message,
                },
            )
        }

        _ => {
            warn!("received unexpected aux packet");
            Ok(())
        }
    }
}

fn process_aux_packets(dma_manager: &mut DmaManager, analyzer: &mut Analyzer,
        kernelmgr: &mut KernelManager, coremgr: &mut CoreManager, repeaters: &mut [repeater::Repeater],
        routing_table: &mut drtio_routing::RoutingTable, rank: &mut u8, router: &mut routing::Router,
        destination: &mut u8) {
    let result =
        drtioaux::recv(0).and_then(|packet| {
            if let Some(packet) = packet.or_else(|| router.get_local_packet()) {
                process_aux_packet(dma_manager, analyzer, kernelmgr, coremgr,
                    repeaters, routing_table, rank, router, destination, packet)
            } else {
                Ok(())
            }
        });
    if let Err(e) = result {
        warn!("aux packet error ({})", e);
    }
}

fn drtiosat_process_errors() {
    let errors = unsafe { csr::drtiosat::protocol_error_read() };
    if errors & 1 != 0 {
        error!("received packet of an unknown type");
    }
    if errors & 2 != 0 {
        error!("received truncated packet");
    }
    if errors & 4 != 0 {
        let destination = unsafe {
            csr::drtiosat::buffer_space_timeout_dest_read()
        };
        error!("timeout attempting to get buffer space from CRI, destination=0x{:02x}", destination)
    }
    let drtiosat_active = unsafe { csr::cri_con::selected_read() == 0 };
    if drtiosat_active {
        // RTIO errors are handled by ksupport and dma manager
        if errors & 8 != 0 {
            let channel;
            let timestamp_event;
            let timestamp_counter;
            unsafe {
                channel = csr::drtiosat::underflow_channel_read();
                timestamp_event = csr::drtiosat::underflow_timestamp_event_read() as i64;
                timestamp_counter = csr::drtiosat::underflow_timestamp_counter_read() as i64;
            }
            error!("write underflow, channel={}, timestamp={}, counter={}, slack={}",
                channel, timestamp_event, timestamp_counter, timestamp_event-timestamp_counter);
        }
        if errors & 16 != 0 {
            error!("write overflow");
        }
    }
    unsafe {
        csr::drtiosat::protocol_error_write(errors);
    }
}


#[cfg(has_rtio_crg)]
fn init_rtio_crg() {
    unsafe {
        csr::rtio_crg::pll_reset_write(0);
    }
    clock::spin_us(150);
    let locked = unsafe { csr::rtio_crg::pll_locked_read() != 0 };
    if !locked {
        error!("RTIO clock failed");
    }
}

#[cfg(not(has_rtio_crg))]
fn init_rtio_crg() { }

fn hardware_tick(ts: &mut u64) {
    let now = clock::get_ms();
    if now > *ts {
        #[cfg(has_grabber)]
        board_artiq::grabber::tick();
        *ts = now + 200;
    }
}

#[cfg(all(has_si5324, rtio_frequency = "125.0"))]
const SI5324_SETTINGS: si5324::FrequencySettings
    = si5324::FrequencySettings {
    n1_hs  : 5,
    nc1_ls : 8,
    n2_hs  : 7,
    n2_ls  : 360,
    n31    : 63,
    n32    : 63,
    bwsel  : 4,
    crystal_as_ckin2: true
};

#[cfg(all(has_si5324, rtio_frequency = "100.0"))]
const SI5324_SETTINGS: si5324::FrequencySettings
    = si5324::FrequencySettings {
    n1_hs  : 5,
    nc1_ls : 10,
    n2_hs  : 10,
    n2_ls  : 250,
    n31    : 50,
    n32    : 50,
    bwsel  : 4,
    crystal_as_ckin2: true
};

#[cfg(all(has_si549, rtio_frequency = "125.0"))]
const SI549_SETTINGS: si549::FrequencySetting = si549::FrequencySetting {
    main: si549::DividerConfig {
        hsdiv: 0x058,
        lsdiv: 0,
        fbdiv: 0x04815791F25,
    },
    helper: si549::DividerConfig {
        // 125MHz*32767/32768
        hsdiv: 0x058,
        lsdiv: 0,
        fbdiv: 0x04814E8F442,
    },
};

#[cfg(all(has_si549, rtio_frequency = "100.0"))]
const SI549_SETTINGS: si549::FrequencySetting = si549::FrequencySetting {
    main: si549::DividerConfig {
        hsdiv: 0x06C,
        lsdiv: 0,
        fbdiv: 0x046C5F49797,
    },
    helper: si549::DividerConfig {
        // 100MHz*32767/32768
        hsdiv: 0x06C,
        lsdiv: 0,
        fbdiv: 0x046C5670BBD,
    },
};

#[cfg(not(any(soc_platform = "efc", soc_platform = "phaser")))]
fn sysclk_setup() {
    let switched = unsafe {
        csr::crg::switch_done_read()
    };
    if switched == 1 {
        info!("Clocking has already been set up.");
        return;
    }
    else {
        #[cfg(has_si5324)]
        si5324::setup(&SI5324_SETTINGS, si5324::Input::Ckin1).expect("cannot initialize Si5324");
        #[cfg(has_si549)]
        si549::main_setup(&SI549_SETTINGS).expect("cannot initialize main Si549");

        info!("Switching sys clock, rebooting...");
        // delay for clean UART log, wait until UART FIFO is empty
        clock::spin_us(3000);
        unsafe {
            csr::gt_drtio::stable_clkin_write(1);
        }
        loop {}
    }
}

fn setup_log_levels() {
    match config::read_str("log_level", |r| r.map(|s| s.parse())) {
        Ok(Ok(log_level_filter)) => {
            info!("log level set to {} by `log_level` config key",
                  log_level_filter);
            log::set_max_level(log_level_filter);
        }
        _ => info!("log level set to INFO by default")
    }
    match config::read_str("uart_log_level", |r| r.map(|s| s.parse())) {
        Ok(Ok(uart_log_level_filter)) => {
            info!("UART log level set to {} by `uart_log_level` config key",
                  uart_log_level_filter);
            logger_artiq::BufferLogger::with(|logger|
                logger.set_uart_log_level(uart_log_level_filter));
        }
        _ => info!("UART log level set to INFO by default")
    }
}

static mut LOG_BUFFER: [u8; 1<<17] = [0; 1<<17];

#[no_mangle]
pub extern fn main() -> i32 {
    extern {
        static mut _fheap: u8;
        static mut _eheap: u8;
        static mut _sstack_guard: u8;
    }

    unsafe {
        ALLOC.add_range(&mut _fheap, &mut _eheap);
        pmp::init_stack_guard(&_sstack_guard as *const u8 as usize);
    }
    #[cfg(soc_platform = "kasli")]
    irq::enable_interrupts();
    #[cfg(has_wrpll)]
    irq::enable(csr::WRPLL_INTERRUPT);

    clock::init();
    unsafe {
        logger_artiq::BufferLogger::new(&mut LOG_BUFFER[..]).register(||
            boot::start_user(startup as usize));
    }

    0
}

fn startup() {
    info!("ARTIQ satellite manager starting...");
    info!("software ident {}", csr::CONFIG_IDENTIFIER_STR);
    info!("gateware ident {}", ident::read(&mut [0; 64]));

    setup_log_levels();

    #[cfg(has_i2c)]
    i2c::init().expect("I2C initialization failed");
    #[cfg(all(soc_platform = "kasli", hw_rev = "v2.0"))]
    let (mut io_expander0, mut io_expander1);
    #[cfg(all(soc_platform = "kasli", hw_rev = "v2.0"))]
    {
        io_expander0 = board_misoc::io_expander::IoExpander::new(0).unwrap();
        io_expander1 = board_misoc::io_expander::IoExpander::new(1).unwrap();
        io_expander0.init().expect("I2C I/O expander #0 initialization failed");
        io_expander1.init().expect("I2C I/O expander #1 initialization failed");

        // Actively drive TX_DISABLE to false on SFP0..3
        io_expander0.set_oe(0, 1 << 1).unwrap();
        io_expander0.set_oe(1, 1 << 1).unwrap();
        io_expander1.set_oe(0, 1 << 1).unwrap();
        io_expander1.set_oe(1, 1 << 1).unwrap();
        io_expander0.set(0, 1, false);
        io_expander0.set(1, 1, false);
        io_expander1.set(0, 1, false);
        io_expander1.set(1, 1, false);
        io_expander0.service().unwrap();
        io_expander1.service().unwrap();
    }

    #[cfg(not(any(soc_platform = "efc", soc_platform = "phaser")))]
    sysclk_setup();

    #[cfg(has_si549)]
    si549::helper_setup(&SI549_SETTINGS).expect("cannot initialize helper Si549");    

    #[cfg(soc_platform = "efc")]
    let mut io_expander;
    #[cfg(soc_platform = "efc")]
    {
        let p3v3_fmc_en_pin;
        let vadj_fmc_en_pin;

        #[cfg(hw_rev = "v1.0")]
        {
            p3v3_fmc_en_pin = 0;
            vadj_fmc_en_pin = 1;
        }
        #[cfg(hw_rev = "v1.1")]
        {
            p3v3_fmc_en_pin = 1;
            vadj_fmc_en_pin = 7;
        }

        io_expander = board_misoc::io_expander::IoExpander::new().unwrap();
        io_expander.init().expect("I2C I/O expander initialization failed");

        // Enable LEDs
        io_expander.set_oe(0, 1 << 5 | 1 << 6 | 1 << 7).unwrap();
        
        // Enable VADJ and P3V3_FMC
        io_expander.set_oe(1, 1 << p3v3_fmc_en_pin | 1 << vadj_fmc_en_pin).unwrap();

        io_expander.set(1, p3v3_fmc_en_pin, true);
        io_expander.set(1, vadj_fmc_en_pin, true);

        io_expander.service().unwrap();
    }

    #[cfg(not(any(soc_platform = "efc", soc_platform = "phaser")))]
    unsafe {
        csr::gt_drtio::txenable_write(0xffffffffu32 as _);
    }

    #[cfg(has_drtio_eem)]
    unsafe {
        csr::eem_transceiver::txenable_write(0xffffffffu32 as _);
    }

    init_rtio_crg();

    config::read_str("sed_spread_enable", |r| {
        match r {
            Ok("1") => { info!("SED spreading enabled"); toggle_sed_spread(1); },
            Ok("0") => { info!("SED spreading disabled"); toggle_sed_spread(0); },
            Ok(_) => { 
                warn!("sed_spread_enable value not supported (only 1, 0 allowed), disabling by default");
                toggle_sed_spread(0);
            },
            Err(_) => { info!("SED spreading disabled by default"); toggle_sed_spread(0) },
        }
    });

    #[cfg(has_drtio_eem)]
    {
        drtio_eem::init();
        unsafe {
            csr::eem_transceiver::rx_ready_write(1)
        }
    }

    #[cfg(has_drtio_routing)]
    let mut repeaters = [repeater::Repeater::default(); csr::DRTIOREP.len()];
    #[cfg(not(has_drtio_routing))]
    let mut repeaters = [repeater::Repeater::default(); 0];
    for i in 0..repeaters.len() {
        repeaters[i] = repeater::Repeater::new(i as u8);
    } 
    let mut routing_table = drtio_routing::RoutingTable::default_empty();
    let mut rank = 1;
    let mut destination = 1;

    let mut hardware_tick_ts = 0;

    #[cfg(soc_platform = "efc")]
    ad9117::init().expect("AD9117 initialization failed");
    
    loop {
        let mut router = routing::Router::new();

        while !drtiosat_link_rx_up() {
            drtiosat_process_errors();
            for rep in repeaters.iter_mut() {
                rep.service(&routing_table, rank, destination, &mut router);
            }
            #[cfg(all(soc_platform = "kasli", hw_rev = "v2.0"))]
            {
                io_expander0.service().expect("I2C I/O expander #0 service failed");
                io_expander1.service().expect("I2C I/O expander #1 service failed");
            }
            #[cfg(soc_platform = "efc")]
            io_expander.service().expect("I2C I/O expander service failed");
            hardware_tick(&mut hardware_tick_ts);
        }

        info!("uplink is up, switching to recovered clock");
        #[cfg(has_si5324)]
        {
            si5324::siphaser::select_recovered_clock(true).expect("failed to switch clocks");
            si5324::siphaser::calibrate_skew().expect("failed to calibrate skew");
        }

        #[cfg(has_wrpll)]
        si549::wrpll::select_recovered_clock(true);

        // various managers created here, so when link is dropped, DMA traces,
        // analyzer logs, kernels are cleared and/or stopped for a clean slate
        // on subsequent connections, without a manual intervention.
        let mut dma_manager = DmaManager::new();
        let mut analyzer = Analyzer::new();
        let mut kernelmgr = KernelManager::new();
        let mut coremgr = CoreManager::new();

        cricon_select(RtioMaster::Drtio);
        drtioaux::reset(0);
        drtiosat_reset(false);
        drtiosat_reset_phy(false);

        while drtiosat_link_rx_up() {
            drtiosat_process_errors();
            process_aux_packets(&mut dma_manager, &mut analyzer, 
                &mut kernelmgr, &mut coremgr, &mut repeaters, &mut routing_table,
                &mut rank, &mut router, &mut destination);
            for rep in repeaters.iter_mut() {
                rep.service(&routing_table, rank, destination, &mut router);
            }
            #[cfg(all(soc_platform = "kasli", hw_rev = "v2.0"))]
            {
                io_expander0.service().expect("I2C I/O expander #0 service failed");
                io_expander1.service().expect("I2C I/O expander #1 service failed");
            }
            #[cfg(soc_platform = "efc")]
            io_expander.service().expect("I2C I/O expander service failed");
            hardware_tick(&mut hardware_tick_ts);
            if drtiosat_tsc_loaded() {
                info!("TSC loaded from uplink");
                for rep in repeaters.iter() {
                    if let Err(e) = rep.sync_tsc() {
                        error!("failed to sync TSC ({})", e);
                    }
                }
                if let Err(e) = drtioaux::send(0, &drtioaux::Packet::TSCAck) {
                    error!("aux packet error: {}", e);
                }
            }
            if let Some(status) = dma_manager.get_status() {
                info!("playback done, error: {}, channel: {}, timestamp: {}", status.error, status.channel, status.timestamp);
                router.route(drtioaux::Packet::DmaPlaybackStatus { 
                    source: destination, destination: status.source, id: status.id,
                    error: status.error, channel: status.channel, timestamp: status.timestamp 
                }, &routing_table, rank, destination);
            }

            kernelmgr.process_kern_requests(&mut router, &routing_table, rank, destination, &mut dma_manager);
            
            #[cfg(has_drtio_routing)]
            if let Some((repno, packet)) = router.get_downstream_packet() {
                if let Err(e) = repeaters[repno].aux_send(&packet) {
                    warn!("[REP#{}] Error when sending packet to satellite ({:?})", repno, e)
                }
            }

            if let Some(packet) = router.get_upstream_packet() {
                drtioaux::send(0, &packet).unwrap();
            }
        }

        drtiosat_reset_phy(true);
        drtiosat_reset(true);
        drtiosat_tsc_loaded();
        info!("uplink is down, switching to local oscillator clock");
        #[cfg(has_si5324)]
        si5324::siphaser::select_recovered_clock(false).expect("failed to switch clocks");
        #[cfg(has_wrpll)]
        si549::wrpll::select_recovered_clock(false);
    }
}

#[cfg(soc_platform = "efc")]
fn enable_error_led() {
    let p3v3_fmc_en_pin;
    let vadj_fmc_en_pin;

    #[cfg(hw_rev = "v1.0")]
    {
        p3v3_fmc_en_pin = 0;
        vadj_fmc_en_pin = 1;
    }
    #[cfg(hw_rev = "v1.1")]
    {
        p3v3_fmc_en_pin = 1;
        vadj_fmc_en_pin = 7;
    }

    let mut io_expander = board_misoc::io_expander::IoExpander::new().unwrap();

    // Keep LEDs enabled
    io_expander.set_oe(0, 1 << 5 | 1 << 6 | 1 << 7).unwrap();
    // Enable Error LED
    io_expander.set(0, 7, true);

    // Keep VADJ and P3V3_FMC enabled
    io_expander.set_oe(1, 1 << p3v3_fmc_en_pin | 1 << vadj_fmc_en_pin).unwrap();

    io_expander.set(1, p3v3_fmc_en_pin, true);
    io_expander.set(1, vadj_fmc_en_pin, true);

    io_expander.service().unwrap();
}

#[no_mangle]
pub extern fn exception(_regs: *const u32) {
    let pc = mepc::read();
    let cause = mcause::read().cause();
    match cause {
        mcause::Trap::Interrupt(_source) => {
            #[cfg(has_wrpll)]
            if irq::is_pending(csr::WRPLL_INTERRUPT) {
                si549::wrpll::interrupt_handler();
            }
        },

        mcause::Trap::Exception(e) => {
            fn hexdump(addr: u32) {
                let addr = (addr - addr % 4) as *const u32;
                let mut ptr  = addr;
                println!("@ {:08p}", ptr);
                for _ in 0..4 {
                    print!("+{:04x}: ", ptr as usize - addr as usize);
                    print!("{:08x} ",   unsafe { *ptr }); ptr = ptr.wrapping_offset(1);
                    print!("{:08x} ",   unsafe { *ptr }); ptr = ptr.wrapping_offset(1);
                    print!("{:08x} ",   unsafe { *ptr }); ptr = ptr.wrapping_offset(1);
                    print!("{:08x}\n",  unsafe { *ptr }); ptr = ptr.wrapping_offset(1);
                }
            }

            hexdump(u32::try_from(pc).unwrap());
            let mtval = mtval::read();
            panic!("exception {:?} at PC 0x{:x}, trap value 0x{:x}", e, u32::try_from(pc).unwrap(), mtval)
        }
    }
}

#[no_mangle]
pub extern fn abort() {
    println!("aborted");
    loop {}
}

#[no_mangle] // https://github.com/rust-lang/rust/issues/{38281,51647}
#[panic_handler]
pub fn panic_fmt(info: &core::panic::PanicInfo) -> ! {
    #[cfg(has_error_led)]
    unsafe {
        csr::error_led::out_write(1);
    }

    if let Some(location) = info.location() {
        print!("panic at {}:{}:{}", location.file(), location.line(), location.column());
        #[cfg(soc_platform = "efc")]
        {
            if location.file() != "libboard_misoc/io_expander.rs" {
                enable_error_led();
            }
        }
    } else {
        print!("panic at unknown location");
        #[cfg(soc_platform = "efc")]
        enable_error_led();
    }
    if let Some(message) = info.message() {
        println!(": {}", message);
    } else {
        println!("");
    }
    loop {}
}
